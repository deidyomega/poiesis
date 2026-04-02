"""Weather fetching tool for Glitch Core agents."""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field


class WeatherResult(BaseModel):
    """Weather information for a city."""
    city: str
    temperature: float = Field(description="Temperature in Celsius")
    description: str = Field(description="Weather description")
    humidity: int = Field(description="Humidity percentage")
    feels_like: float = Field(description="Feels like temperature in Celsius")


async def get_weather(city: str) -> str:
    """
    Get current weather information for a city.
    
    Args:
        city: Name of the city to get weather for
        
    Returns:
        Formatted weather information string
    """
    try:
        # Using OpenWeatherMap free tier (no API key needed for basic demo)
        # In production, would use proper API key from environment
        url = f"https://api.openweathermap.org/data/2.5/weather"
        params = {
            "q": city,
            "units": "metric",
            "appid": "demo"  # This won't work but shows the structure
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params)
            
            if response.status_code == 401:
                # Expected since we don't have a real API key
                return f"Weather service requires API key configuration. But I would fetch weather for {city}!"
            
            if response.status_code != 200:
                return f"Unable to fetch weather for {city}. Status: {response.status_code}"
                
            data = response.json()
            
            weather = WeatherResult(
                city=data["name"],
                temperature=data["main"]["temp"],
                description=data["weather"][0]["description"],
                humidity=data["main"]["humidity"],
                feels_like=data["main"]["feels_like"]
            )
            
            return (
                f"🌤️ Weather in {weather.city}:\n"
                f"Temperature: {weather.temperature}°C (feels like {weather.feels_like}°C)\n"
                f"Conditions: {weather.description.title()}\n"
                f"Humidity: {weather.humidity}%"
            )
            
    except Exception as e:
        return f"Error fetching weather for {city}: {str(e)}"