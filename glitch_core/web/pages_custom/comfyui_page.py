from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import httpx
import asyncio
import uuid
import json
import base64
from typing import Optional

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# ComfyUI server URL on Tailscale
COMFYUI_URL = "http://comfyui:8188"

class ImageGenRequest(BaseModel):
    prompt: str
    negative_prompt: Optional[str] = ""
    steps: int = 20
    cfg_scale: float = 8.0

@router.get("/comfyui", response_class=HTMLResponse)
async def comfyui_page(request: Request):
    """ComfyUI image generation interface"""
    return templates.TemplateResponse("comfyui_page.html", {"request": request})

@router.get("/comfyui/status")
async def check_comfyui_status():
    """Check if ComfyUI server is accessible"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{COMFYUI_URL}/system_stats", timeout=5.0)
            return {"status": "online", "data": response.json()}
    except Exception as e:
        return {"status": "offline", "error": str(e)}

@router.post("/comfyui/generate")
async def generate_image(request: ImageGenRequest):
    """Generate an image using ComfyUI"""
    client_id = str(uuid.uuid4())
    
    # Basic SDXL workflow
    workflow = {
        "3": {
            "inputs": {
                "seed": 42,
                "steps": request.steps,
                "cfg": request.cfg_scale,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0]
            },
            "class_type": "KSampler"
        },
        "4": {
            "inputs": {
                "ckpt_name": "sd_xl_base_1.0.safetensors"
            },
            "class_type": "CheckpointLoaderSimple"
        },
        "5": {
            "inputs": {
                "width": 1024,
                "height": 1024,
                "batch_size": 1
            },
            "class_type": "EmptyLatentImage"
        },
        "6": {
            "inputs": {
                "text": request.prompt,
                "clip": ["4", 1]
            },
            "class_type": "CLIPTextEncode"
        },
        "7": {
            "inputs": {
                "text": request.negative_prompt,
                "clip": ["4", 1]
            },
            "class_type": "CLIPTextEncode"
        },
        "8": {
            "inputs": {
                "samples": ["3", 0],
                "vae": ["4", 2]
            },
            "class_type": "VAEDecode"
        },
        "9": {
            "inputs": {
                "filename_prefix": "ComfyUI",
                "images": ["8", 0]
            },
            "class_type": "SaveImage"
        }
    }
    
    try:
        async with httpx.AsyncClient() as client:
            # Queue the prompt
            response = await client.post(
                f"{COMFYUI_URL}/prompt",
                json={"prompt": workflow, "client_id": client_id}
            )
            
            if response.status_code != 200:
                raise HTTPException(status_code=500, detail=f"ComfyUI error: {response.text}")
            
            prompt_id = response.json()["prompt_id"]
            
            # Poll for completion
            for _ in range(60):  # 60 second timeout
                await asyncio.sleep(1)
                
                history_response = await client.get(f"{COMFYUI_URL}/history/{prompt_id}")
                if history_response.status_code == 200:
                    history = history_response.json()
                    if prompt_id in history:
                        outputs = history[prompt_id]["outputs"]
                        if "9" in outputs and "images" in outputs["9"]:
                            # Get the first generated image
                            image_data = outputs["9"]["images"][0]
                            image_name = image_data["filename"]
                            
                            # Fetch the actual image
                            image_response = await client.get(
                                f"{COMFYUI_URL}/view?filename={image_name}"
                            )
                            
                            if image_response.status_code == 200:
                                # Convert to base64 for easy display
                                image_b64 = base64.b64encode(image_response.content).decode()
                                return {
                                    "status": "success",
                                    "image": f"data:image/png;base64,{image_b64}",
                                    "prompt_id": prompt_id
                                }
            
            return {"status": "timeout", "error": "Generation timed out after 60 seconds"}
    
    except httpx.RequestError as e:
        return {"status": "error", "error": f"Connection error: {str(e)}"}