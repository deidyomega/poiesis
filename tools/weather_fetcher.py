"""Weather fetching tool for Glitch Core."""
from __future__ import annotations

import json
import urllib.request
import urllib.parse
from typing import Any


def get_weather(city: str) -> str:
    """
    Fetch current weather for a city using a free weather API.
    
    Args:
        city: Name of the city to get weather for
        
    Returns:
        Weather information as a formatted string
    """
    try:
        # Using wttr.in - a simple weather service that doesn't require API keys
        # Format: plain text, single line
        city_encoded = urllib.parse.quote(city)
        url = f"https://wttr.in/{city_encoded}?format=3"
        
        with urllib.request.urlopen(url, timeout=10) as response:
            weather_data = response.read().decode('utf-8').strip()
            
        if weather_data and not weather_data.startswith("Unknown location"):
            return f"🌤️ Weather for {city}: {weather_data}"
        else:
            return f"❌ Sorry, I couldn't find weather data for '{city}'. Please check the city name."
            
    except Exception as e:
        return f"❌ Failed to fetch weather data: {str(e)}"


# PydanticAI tool interface
def weather_tool(city: str) -> str:
    """Get current weather for a city."""
    return get_weather(city)