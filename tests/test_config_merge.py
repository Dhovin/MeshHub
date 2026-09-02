import unittest
import unittest.mock
import os
import sys
import importlib.util

from importlib.machinery import SourceFileLoader

# Load bin/meshhub dynamically as a module since it doesn't have a .py extension
script_dir = os.path.dirname(os.path.abspath(__file__))
meshhub_path = os.path.abspath(os.path.join(script_dir, "../bin/meshhub"))

loader = SourceFileLoader("meshhub", meshhub_path)
spec = importlib.util.spec_from_file_location("meshhub", meshhub_path, loader=loader)
meshhub = importlib.util.module_from_spec(spec)
sys.modules["meshhub"] = meshhub
loader.exec_module(meshhub)
meshbot = meshhub

class TestConfigMerge(unittest.TestCase):
    def test_recursive_dict_merge(self):
        upstream = {
            "core": {
                "timeSyncInterval": "0 0 * * *",
                "shutdownTimeoutMs": 10000,
                "ipcPort": 5002
            },
            "modules": {
                "template": {
                    "enabled": False,
                    "messagePrefix": "[MeshHub]"
                }
            }
        }
        local = {
            "core": {
                "shutdownTimeoutMs": 20000,
                "ipcPort": 5003
            },
            "modules": {
                "template": {
                    "enabled": True
                }
            }
        }
        expected = {
            "core": {
                "timeSyncInterval": "0 0 * * *",
                "shutdownTimeoutMs": 20000,
                "ipcPort": 5003
            },
            "modules": {
                "template": {
                    "enabled": True,
                    "messagePrefix": "[MeshHub]"
                }
            }
        }
        result = meshbot.smart_merge_config(upstream, local)
        self.assertEqual(result, expected)

    def test_primitive_list_merge(self):
        upstream = ["#test", "#testing"]
        local = ["#testing", "#weather"]
        result = meshbot.smart_merge_config(upstream, local)
        # Order should preserve upstream first, then local additions
        self.assertEqual(result, ["#test", "#testing", "#weather"])

    def test_broker_merge_rules(self):
        upstream = [
            {
                "server": "mqtt-us-v1.letsmesh.net",
                "port": 8883,
                "use_tls": True
            },
            {
                "server": "mqtt-eu-v1.letsmesh.net",
                "port": 443,
                "use_tls": True
            },
            {
                "server": "private.broker.com",
                "port": 1883,
                "use_tls": False
            }
        ]
        local = [
            {
                "server": "mqtt-us-v1.letsmesh.net",
                "port": 443,  # Outdated port locally
                "use_tls": False,
                "custom_local_tag": "test"
            },
            {
                "server": "private.broker.com",
                "port": 1884,  # User changed port for private broker
                "use_tls": True
            },
            {
                "server": "my-other-private.net",
                "port": 1883,
                "use_tls": False
            }
        ]
        
        result = meshbot.smart_merge_config(upstream, local)
        
        # We index the result by server to check properties
        result_map = {b["server"]: b for b in result}
        
        # 1. public broker letsmesh.net should favor upstream (port 8883, use_tls True)
        # but retain non-conflicting local keys (custom_local_tag)
        self.assertEqual(result_map["mqtt-us-v1.letsmesh.net"]["port"], 8883)
        self.assertTrue(result_map["mqtt-us-v1.letsmesh.net"]["use_tls"])
        self.assertEqual(result_map["mqtt-us-v1.letsmesh.net"]["custom_local_tag"], "test")
        
        # 2. new upstream broker mqtt-eu-v1.letsmesh.net should be present
        self.assertIn("mqtt-eu-v1.letsmesh.net", result_map)
        self.assertEqual(result_map["mqtt-eu-v1.letsmesh.net"]["port"], 443)
        
        # 3. private broker private.broker.com should favor local settings (port 1884, use_tls True)
        self.assertEqual(result_map["private.broker.com"]["port"], 1884)
        self.assertTrue(result_map["private.broker.com"]["use_tls"])
        
        # 4. private local broker my-other-private.net should be present
        self.assertIn("my-other-private.net", result_map)
        self.assertEqual(result_map["my-other-private.net"]["port"], 1883)

    def test_primitive_overrides(self):
        upstream = "some_string"
        local = 12345
        result = meshbot.smart_merge_config(upstream, local)
        self.assertEqual(result, 12345)

    @unittest.mock.patch('subprocess.run')
    @unittest.mock.patch('core.validator.validate')
    @unittest.mock.patch('builtins.open', new_callable=unittest.mock.mock_open, read_data='{}')
    @unittest.mock.patch('os.path.exists')
    def test_auto_merge_config_json_success(self, mock_exists, mock_open, mock_validate, mock_run):
        import subprocess
        mock_exists.return_value = True
        mock_validate.return_value = [] # no validation errors
        
        call_count = [0]
        def run_side_effect(cmd, *args, **kwargs):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if "status --porcelain" in cmd_str:
                call_count[0] += 1
                if call_count[0] == 1:
                    return subprocess.CompletedProcess(cmd, 0, stdout="UU config/config.json\n")
                else:
                    return subprocess.CompletedProcess(cmd, 0, stdout="")
            elif "show :2:config/config.json" in cmd_str:
                return subprocess.CompletedProcess(cmd, 0, stdout='{"core": {"ipcPort": 5002}}')
            elif "show :3:config/config.json" in cmd_str:
                return subprocess.CompletedProcess(cmd, 0, stdout='{"core": {"ipcPort": 5003}}')
            return subprocess.CompletedProcess(cmd, 0, stdout="")
            
        mock_run.side_effect = run_side_effect
        
        res = meshbot.auto_merge_config_json()
        self.assertTrue(res)
        mock_open.assert_called_with("config/config.json", "w", encoding="utf-8")

    @unittest.mock.patch('subprocess.run')
    @unittest.mock.patch('core.validator.validate')
    @unittest.mock.patch('builtins.open', new_callable=unittest.mock.mock_open, read_data='{}')
    @unittest.mock.patch('os.path.exists')
    def test_auto_merge_config_json_remaining_conflicts(self, mock_exists, mock_open, mock_validate, mock_run):
        import subprocess
        mock_exists.return_value = True
        mock_validate.return_value = []
        
        def run_side_effect(cmd, *args, **kwargs):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if "status --porcelain" in cmd_str:
                # Always returns UU config/config.json and UU other_file.py
                return subprocess.CompletedProcess(cmd, 0, stdout="UU config/config.json\nUU other_file.py\n")
            elif "show :2:config/config.json" in cmd_str:
                return subprocess.CompletedProcess(cmd, 0, stdout='{"core": {"ipcPort": 5002}}')
            elif "show :3:config/config.json" in cmd_str:
                return subprocess.CompletedProcess(cmd, 0, stdout='{"core": {"ipcPort": 5003}}')
            return subprocess.CompletedProcess(cmd, 0, stdout="")
            
        mock_run.side_effect = run_side_effect
        
        res = meshbot.auto_merge_config_json()
        self.assertFalse(res) # remaining conflicts should return False from auto_merge_config_json

    @unittest.mock.patch('subprocess.run')
    def test_auto_merge_config_json_not_conflicted(self, mock_run):
        import subprocess
        mock_run.return_value = subprocess.CompletedProcess(["git", "status", "--porcelain"], 0, stdout="")
        res = meshbot.auto_merge_config_json()
        self.assertFalse(res)

    @unittest.mock.patch('subprocess.run')
    @unittest.mock.patch('core.validator.validate')
    @unittest.mock.patch('builtins.open', new_callable=unittest.mock.mock_open, read_data='{}')
    @unittest.mock.patch('os.path.exists')
    def test_auto_merge_config_json_validation_fails(self, mock_exists, mock_open, mock_validate, mock_run):
        import subprocess
        mock_exists.return_value = True
        mock_validate.return_value = ["Required key connection missing"]
        
        def run_side_effect(cmd, *args, **kwargs):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if "status --porcelain" in cmd_str:
                return subprocess.CompletedProcess(cmd, 0, stdout="UU config/config.json\n")
            elif "show :2:config/config.json" in cmd_str:
                return subprocess.CompletedProcess(cmd, 0, stdout='{"core": {"ipcPort": 5002}}')
            elif "show :3:config/config.json" in cmd_str:
                return subprocess.CompletedProcess(cmd, 0, stdout='{"core": {"ipcPort": 5003}}')
            return subprocess.CompletedProcess(cmd, 0, stdout="")
            
        mock_run.side_effect = run_side_effect
        
        res = meshbot.auto_merge_config_json()
        self.assertFalse(res)
    @unittest.mock.patch('core.validator.validate')
    @unittest.mock.patch('builtins.open', new_callable=unittest.mock.mock_open, read_data='{"modules": {"weather_bot": {"enabled": true}}}')
    @unittest.mock.patch('os.path.exists')
    def test_set_module_enabled(self, mock_exists, mock_open, mock_validate):
        mock_exists.return_value = True
        mock_validate.return_value = []
        
        # Test disabling
        meshbot.set_module_enabled("weather_bot", False)
        
        # Verify it opened config/config.json for writing
        mock_open.assert_any_call("config/config.json", "w")
        
        # Verify it sets enabled to False in the written data
        handle = mock_open()
        written_data = "".join(call.args[0] for call in handle.write.call_args_list)
        import json
        written_config = json.loads(written_data)
        self.assertFalse(written_config["modules"]["weather_bot"]["enabled"])

if __name__ == '__main__':
    unittest.main()
