import requests
import asyncio
from plugins.base_plugin import BasePlugin


class Plugin(BasePlugin):
    plugin_name = "weather"

    @property
    def description(self):
        return "Show weather forecast for a radio node using GPS location"

    def generate_forecast(self, latitude, longitude):
        url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&hourly=temperature_2m,precipitation_probability,weathercode,cloudcover&forecast_days=1&current_weather=true"

        try:
            response = requests.get(url, timeout=10)
            data = response.json()

            # Extract relevant weather data
            current_temp = data["current_weather"]["temperature"]
            current_weather_code = data["current_weather"]["weathercode"]
            is_day = data["current_weather"]["is_day"]

            forecast_2h_temp = data["hourly"]["temperature_2m"][2]
            forecast_2h_precipitation = data["hourly"]["precipitation_probability"][2]
            forecast_2h_weather_code = data["hourly"]["weathercode"][2]

            forecast_5h_temp = data["hourly"]["temperature_2m"][5]
            forecast_5h_precipitation = data["hourly"]["precipitation_probability"][5]
            forecast_5h_weather_code = data["hourly"]["weathercode"][5]

            def weather_code_to_text(weather_code, is_day):
                weather_mapping = {
                    0: "☀️ Sunny" if is_day else "🌙 Clear",
                    1: "⛅️ Partly Cloudy" if is_day else "🌙⛅️ Clear",
                    2: "🌤️ Mostly Clear" if is_day else "🌙🌤️ Mostly Clear",
                    3: "🌥️ Mostly Cloudy" if is_day else "🌙🌥️ Mostly Clear",
                    45: "🌫️ Foggy" if is_day else "🌙🌫️ Foggy",
                    48: "🌫️ Foggy" if is_day else "🌙🌫️ Foggy",
                    51: "🌧️ Light Drizzle",
                    53: "🌧️ Moderate Drizzle",
                    55: "🌧️ Heavy Drizzle",
                    56: "🌧️ Freezing Drizzle",
                    57: "🌧️ Freezing Drizzle",
                    61: "🌧️ Light Rain",
                    63: "🌧️ Moderate Rain",
                    65: "🌧️ Heavy Rain",
                    66: "🌧️ Freezing Rain",
                    67: "🌧️ Freezing Rain",
                    71: "❄️ Light Snow",
                    73: "❄️ Moderate Snow",
                    75: "❄️ Heavy Snow",
                    77: "❄️ Snow Grains",
                    80: "🌧️ Light Rain Showers",
                    81: "🌧️ Moderate Rain Showers",
                    82: "🌧️ Heavy Rain Showers",
                    85: "❄️ Light Snow Showers",
                    86: "❄️ Heavy Snow Showers",
                    95: "⛈️ Thunderstorm",
                    96: "⛈️ Thunderstorm with Hail",
                    99: "⛈️ Thunderstorm with Hail",
                }

                return weather_mapping.get(weather_code, "❓ Unknown")

            # Generate one-line weather forecast
            forecast = f"Now: {weather_code_to_text(current_weather_code, is_day)} - {current_temp}°C | "
            forecast += f"+2h: {weather_code_to_text(forecast_2h_weather_code, is_day)} - {forecast_2h_temp}°C {forecast_2h_precipitation}% | "
            forecast += f"+5h: {weather_code_to_text(forecast_5h_weather_code, is_day)} - {forecast_5h_temp}°C {forecast_5h_precipitation}%"

            return forecast

        except requests.exceptions.RequestException as e:
            print(f"Error: {e}")
            return None

    async def handle_meshtastic_message(
        self, packet, formatted_message, longname, meshnet_name
    ):
        if (
            "decoded" in packet
            and "portnum" in packet["decoded"]
            and packet["decoded"]["portnum"] == "TEXT_MESSAGE_APP"
            and "text" in packet["decoded"]
        ):
            message = packet["decoded"]["text"]
            message = message.strip()
            channel = packet.get("channel", 0)  # Default to channel 0 if not provided

            if not self.is_channel_enabled(channel):
                self.logger.debug(f"Channel {channel} not enabled for plugin '{self.plugin_name}'")
                return False

            if f"!{self.plugin_name}" not in message:
                return False

            from meshtastic_utils import connect_meshtastic

            meshtastic_client = connect_meshtastic()
            if packet["fromId"] in meshtastic_client.nodes:
                weather_notice = "Cannot determine location"
                requesting_node = meshtastic_client.nodes.get(packet["fromId"])
                if (
                    requesting_node
                    and "position" in requesting_node
                    and "latitude" in requesting_node["position"]
                    and "longitude" in requesting_node["position"]
                ):
                    weather_notice = self.generate_forecast(
                        latitude=requesting_node["position"]["latitude"],
                        longitude=requesting_node["position"]["longitude"],
                    )

                # Wait for the response delay
                await asyncio.sleep(self.get_response_delay())

                meshtastic_client.sendText(
                    text=weather_notice,
                    destinationId=packet["fromId"],
                )
            return True

    def get_matrix_commands(self):
        return []

    def get_mesh_commands(self):
        return [self.plugin_name]

    async def handle_room_message(self, room, event, full_message):
        return False
