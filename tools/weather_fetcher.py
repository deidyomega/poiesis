"""Simple weather information tool for cities."""

from typing import Any
import random

def get_weather(city: str) -> str:
    """Get weather information for a city.
    
    Args:
        city: The city name to get weather for
        
    Returns:
        A string describing the current weather
    """
    # Since this is a demo tool, we'll return mock weather data
    # In a real implementation, this would call a weather API
    
    weather_conditions = [
        "sunny", "cloudy", "rainy", "partly cloudy", "overcast", "foggy"
    ]
    
    temperatures = list(range(45, 85))  # Fahrenheit
    
    condition = random.choice(weather_conditions)
    temp = random.choice(temperatures)
    
    return f"The weather in {city} is currently {condition} with a temperature of {temp}°F."