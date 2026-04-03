"""ComfyUI Image Generation Page"""
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import httpx
import json
import asyncio
from typing import Optional
import base64

router = APIRouter()

# ComfyUI server on Tailscale network
COMFYUI_URL = "http://comfyui:8188"  # Adjust hostname/port as needed

async def check_comfyui_status():
    """Check if ComfyUI server is reachable"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{COMFYUI_URL}/system_stats", timeout=5.0)
            return response.status_code == 200
    except:
        return False

async def generate_image(prompt: str, negative_prompt: str = "", steps: int = 20, cfg: float = 7.0):
    """Submit generation job to ComfyUI"""
    # Basic text2img workflow - adjust based on your ComfyUI setup
    workflow = {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 8566257,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.00,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0]
            }
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {
                "ckpt_name": "sd_xl_base_1.0.safetensors"  # Adjust to your model
            }
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": 1024,
                "height": 1024,
                "batch_size": 1
            }
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": prompt,
                "clip": ["4", 1]
            }
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": negative_prompt,
                "clip": ["4", 1]
            }
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["3", 0],
                "vae": ["4", 2]
            }
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": "glitch_gen",
                "images": ["8", 0]
            }
        }
    }
    
    async with httpx.AsyncClient() as client:
        # Queue the prompt
        response = await client.post(
            f"{COMFYUI_URL}/prompt",
            json={"prompt": workflow},
            timeout=30.0
        )
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail="Failed to queue generation")
        
        prompt_id = response.json()["prompt_id"]
        
        # Poll for completion (simplified - in production use websockets)
        for _ in range(60):  # 60 second timeout
            await asyncio.sleep(1)
            history = await client.get(f"{COMFYUI_URL}/history/{prompt_id}")
            if history.status_code == 200 and prompt_id in history.json():
                # Get the output images
                outputs = history.json()[prompt_id]["outputs"]
                for node_id, node_output in outputs.items():
                    if "images" in node_output:
                        # Return first generated image
                        image_data = node_output["images"][0]
                        filename = image_data["filename"]
                        
                        # Fetch the actual image
                        img_response = await client.get(
                            f"{COMFYUI_URL}/view",
                            params={"filename": filename}
                        )
                        if img_response.status_code == 200:
                            return base64.b64encode(img_response.content).decode()
                break
        
        raise HTTPException(status_code=500, detail="Generation timed out")

@router.get("/comfyui")
async def comfyui_page(request: Request):
    """ComfyUI Image Generation Interface"""
    return request.app.state.templates.TemplateResponse(
        "comfyui_gen.html",
        {"request": request}
    )

@router.post("/comfyui/generate")
async def generate_endpoint(
    prompt: str = Form(...),
    negative_prompt: str = Form(""),
    steps: int = Form(20),
    cfg: float = Form(7.0)
):
    """API endpoint for image generation"""
    try:
        # Check if ComfyUI is available
        if not await check_comfyui_status():
            return JSONResponse(
                status_code=503,
                content={"error": "ComfyUI server is not reachable"}
            )
        
        # Generate image
        image_b64 = await generate_image(prompt, negative_prompt, steps, cfg)
        
        return JSONResponse({
            "success": True,
            "image": f"data:image/png;base64,{image_b64}"
        })
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )

@router.get("/comfyui/status")
async def status_endpoint():
    """Check ComfyUI server status"""
    is_online = await check_comfyui_status()
    return JSONResponse({
        "online": is_online,
        "server": COMFYUI_URL
    })