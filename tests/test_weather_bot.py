import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta
from modules.weather_bot import (
    get_emoji_for_forecast,
    format_compressed_forecast,
    split_string_to_byte_chunks,
    shorten_to_bytes,
    calculate_heading_and_distance,
    degrees_to_compass8,
    WeatherBot
)

class TestWeatherBotUtils(unittest.TestCase):

    def test_degrees_to_compass8(self):
        self.assertEqual(degrees_to_compass8(0), 'N')
        self.assertEqual(degrees_to_compass8(360), 'N')
        self.assertEqual(degrees_to_compass8(45), 'NE')
        self.assertEqual(degrees_to_compass8(90), 'E')
        self.assertEqual(degrees_to_compass8(180), 'S')
        self.assertEqual(degrees_to_compass8(220), 'SW')
        self.assertEqual(degrees_to_compass8(270), 'W')
        self.assertEqual(degrees_to_compass8(315), 'NW')

    def test_calculate_heading_and_distance(self):
        # Justin, TX coordinates: 33.0906, -97.2911
        # Denton, TX coordinates: 33.2148, -97.1331
        res = calculate_heading_and_distance(33.0906, -97.2911, 33.2148, -97.1331)
        self.assertEqual(res["heading"], "NE")
        self.assertTrue(10.0 < res["distance"] < 25.0) # distance is around ~20.2km

    def test_get_emoji_for_forecast(self):
        self.assertEqual(get_emoji_for_forecast("Thunderstorms"), "⛈️")
        self.assertEqual(get_emoji_for_forecast("Heavy rain and wind"), "🌧️")
        self.assertEqual(get_emoji_for_forecast("Heavy Snow"), "❄️")
        self.assertEqual(get_emoji_for_forecast("Patchy Fog"), "🌫️")
        self.assertEqual(get_emoji_for_forecast("Windy and Sunny"), "💨")
        self.assertEqual(get_emoji_for_forecast("Mostly Sunny"), "🌤️")
        self.assertEqual(get_emoji_for_forecast("Sunny"), "☀️")
        self.assertEqual(get_emoji_for_forecast("Overcast"), "☁️")
        self.assertEqual(get_emoji_for_forecast("Random"), "⛅")

    def test_shorten_to_bytes(self):
        text = "Hello world! This is a long message."
        # Fits in limit
        self.assertEqual(shorten_to_bytes(text, 100), text)
        # Truncates at whitespace
        shortened = shorten_to_bytes(text, 20)
        self.assertTrue(len(shortened.encode('utf-8')) <= 20)
        self.assertEqual(shortened, "Hello world! This")

    def test_split_string_to_byte_chunks(self):
        text = "Line 1. Line 2. Line 3. Line 4."
        chunks = split_string_to_byte_chunks(text, 15)
        # Each chunk should be <= 15 bytes and split on sentence/space boundary
        for chunk in chunks:
            self.assertTrue(len(chunk.encode('utf-8')) <= 15)
        self.assertEqual(chunks[0], "Line 1.")
        self.assertEqual(chunks[1], "Line 2.")

    def test_format_compressed_forecast(self):
        periods = [
            {
                "startTime": "2026-06-10T08:00:00-05:00",
                "isDaytime": True,
                "temperature": 85,
                "shortForecast": "Mostly Sunny"
            },
            {
                "startTime": "2026-06-10T20:00:00-05:00",
                "isDaytime": False,
                "temperature": 69,
                "shortForecast": "Mostly Clear"
            },
            {
                "startTime": "2026-06-11T08:00:00-05:00",
                "isDaytime": True,
                "temperature": 82,
                "shortForecast": "Thunderstorms"
            },
            {
                "startTime": "2026-06-11T20:00:00-05:00",
                "isDaytime": False,
                "temperature": 69,
                "shortForecast": "Scattered Showers"
            },
            {
                "startTime": "2026-06-12T08:00:00-05:00",
                "isDaytime": True,
                "temperature": 86,
                "shortForecast": "Thunderstorms"
            },
            {
                "startTime": "2026-06-12T20:00:00-05:00",
                "isDaytime": False,
                "temperature": 74,
                "shortForecast": "Partly Cloudy"
            }
        ]
        
        forecast_str = format_compressed_forecast("76246", periods)
        self.assertTrue("Wx 76246:" in forecast_str)
        self.assertTrue("today: 🌤️ hi: 85 low: 69" in forecast_str)
        # 2026-06-11 is Thursday. Thursday is "Thur" in weekdays
        self.assertTrue("Thur: ⛈️ hi: 82 low: 69" in forecast_str)
        # 2026-06-12 is Friday. Friday is "Fri" in weekdays
        self.assertTrue("Fri: ⛈️ hi: 86 low: 74" in forecast_str)

class TestWeatherBotMessages(unittest.TestCase):
    def setUp(self):
        self.bot = WeatherBot()
        self.api = MagicMock()
        self.api.is_self = MagicMock(return_value=False)
        self.api.matches_channel = AsyncMock(return_value=False)
        
        self.conn_manager = MagicMock()
        self.conn_manager.execute = AsyncMock(return_value={"ok": True})
        self.api.bot.connection_manager = self.conn_manager
        
        self.bot.api = self.api
        self.bot.channel_names = {"weather": "weather"}

    def test_help_menu_dm(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # We want to test both "help" and "menu"
            for cmd in ("help", "menu", "Help", "MENU"):
                self.conn_manager.execute.reset_mock()
                loop.run_until_complete(
                    self.bot._handle_message_async(sender="Alice", text=cmd, channel=None)
                )
                
                # Check that execute was called with DM message commands
                self.assertTrue(self.conn_manager.execute.called)
                args = self.conn_manager.execute.call_args_list[0][0][0]
                self.assertEqual(args[0], "msg")
                self.assertEqual(args[1], "Alice")
                self.assertIn("WeatherBot Menu:", args[2])
        finally:
            loop.close()

    def test_unsubscribe_dm(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Stub subscriber functions
            self.bot.read_subscriptions = MagicMock(return_value={"Alice": {}})
            self.bot.write_subscriptions = MagicMock()
            
            for cmd in ("unsubscribe", "unsub", "Unsubscribe", "UNSUB"):
                self.conn_manager.execute.reset_mock()
                self.bot.read_subscriptions.reset_mock()
                self.bot.write_subscriptions.reset_mock()
                
                # Setup sub state
                self.bot.read_subscriptions.return_value = {"Alice": {}}
                
                loop.run_until_complete(
                    self.bot._handle_message_async(sender="Alice", text=cmd, channel=None)
                )
                
                # Check subscription removed and confirmation message sent
                self.bot.write_subscriptions.assert_called_once_with({})
                self.assertTrue(self.conn_manager.execute.called)
                args = self.conn_manager.execute.call_args_list[0][0][0]
                self.assertEqual(args[0], "msg")
                self.assertEqual(args[1], "Alice")
                self.assertIn("Unsubscribed", args[2])
        finally:
            loop.close()

class TestWeatherBotAlerts(unittest.TestCase):
    def setUp(self):
        self.bot = WeatherBot()
        self.api = MagicMock()
        self.api.is_self = MagicMock(return_value=False)
        self.api.matches_channel = AsyncMock(return_value=True)
        self.api.request_channel = AsyncMock(return_value=2)
        
        self.conn_manager = MagicMock()
        self.conn_manager.execute = AsyncMock(return_value={"ok": True})
        self.api.bot.connection_manager = self.conn_manager
        
        self.bot.api = self.api
        self.bot.channel_names = {"alerts": "weather", "weather": "weather"}
        self.bot.my_position = {"lat": 33.0906, "lon": -97.2911}

    @patch('modules.weather_bot.requests.get')
    def test_meteo_alerts_filters_to_storms_only(self, mock_get):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Mock NWS response with a mix of storm and non-storm alerts
            future_expiry = (datetime.now().astimezone() + timedelta(hours=2)).isoformat()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "features": [
                    {
                        "id": "alert-1",
                        "properties": {
                            "identifier": "alert-1",
                            "areaDesc": "Denton County",
                            "event": "Severe Thunderstorm Warning",
                            "onset": "2026-08-22T20:00:00-05:00",
                            "expires": future_expiry,
                            "severity": "Severe",
                            "certainty": "Observed",
                            "headline": "Severe Thunderstorm Warning for Denton County",
                            "instruction": "Take shelter."
                        }
                    },
                    {
                        "id": "alert-2",
                        "properties": {
                            "identifier": "alert-2",
                            "areaDesc": "Wise County",
                            "event": "Tornado Warning",
                            "onset": "2026-08-22T20:00:00-05:00",
                            "expires": future_expiry,
                            "severity": "Extreme",
                            "certainty": "Observed",
                            "headline": "Tornado Warning for Wise County",
                            "instruction": "Take cover immediately."
                        }
                    },
                    {
                        "id": "alert-3",
                        "properties": {
                            "identifier": "alert-3",
                            "areaDesc": "Cooke County",
                            "event": "Flash Flood Warning",
                            "onset": "2026-08-22T20:00:00-05:00",
                            "expires": future_expiry,
                            "severity": "Severe",
                            "certainty": "Observed",
                            "headline": "Flash Flood Warning for Cooke County",
                            "instruction": "Turn around don't drown."
                        }
                    },
                    {
                        "id": "alert-4",
                        "properties": {
                            "identifier": "alert-4",
                            "areaDesc": "Dallas County",
                            "event": "Excessive Heat Warning",
                            "onset": "2026-08-22T20:00:00-05:00",
                            "expires": future_expiry,
                            "severity": "Extreme",
                            "certainty": "Observed",
                            "headline": "Excessive Heat Warning",
                            "instruction": "Stay hydrated."
                        }
                    }
                ]
            }
            mock_get.return_value = mock_response
            
            with patch('asyncio.sleep', AsyncMock()):
                loop.run_until_complete(self.bot.check_meteo_alerts())
                
            # Only alert-1 (Severe Thunderstorm) and alert-2 (Tornado) should be broadcasted
            self.assertIn("alert-1", self.bot.meteo_alerts)
            self.assertIn("alert-2", self.bot.meteo_alerts)
            self.assertNotIn("alert-3", self.bot.meteo_alerts)
            self.assertNotIn("alert-4", self.bot.meteo_alerts)
            
            # Verify sent messages on channel 2
            sent_texts = [call[0][0][2] for call in self.conn_manager.execute.call_args_list if call[0][0][0] == "chan"]
            self.assertEqual(len(sent_texts), 2)
            self.assertTrue(any("Severe Thunderstorm Warning" in msg for msg in sent_texts))
            self.assertTrue(any("Tornado Warning" in msg for msg in sent_texts))
            self.assertFalse(any("Flash Flood" in msg for msg in sent_texts))
            self.assertFalse(any("Excessive Heat" in msg for msg in sent_texts))
        finally:
            loop.close()

    def test_blitz_warning_lightning_alert(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Add at least 10 blitz strikes to trigger warning threshold
            for _ in range(10):
                self.bot.blitz_buffer.append({
                    "key": "N|2",
                    "heading": "N",
                    "distance": 20.0,
                    "lat": 33.27,
                    "lon": -97.29
                })
                
            with patch.object(self.bot, 'geocode_cached', AsyncMock(return_value="Denton, TX")), \
                 patch('asyncio.sleep', AsyncMock()):
                loop.run_until_complete(self.bot.blitz_warning())
                
            self.assertTrue(self.conn_manager.execute.called)
            args = self.conn_manager.execute.call_args[0][0]
            self.assertEqual(args[0], "chan")
            self.assertEqual(args[1], "2")
            self.assertIn("🌩️ Lightning: Denton, TX (20km North)", args[2])
        finally:
            loop.close()

if __name__ == '__main__':
    unittest.main()
