from setuptools import setup

setup(
    data_files=[
        ('share/openhop/plugins/openhop.meshhub-bot', [
            'openhop_meshhub_bot/share/openhop/plugins/openhop.meshhub-bot/openhop-plugin.json',
            'openhop_meshhub_bot/share/openhop/plugins/openhop.meshhub-bot/config.default.json',
            'openhop_meshhub_bot/share/openhop/plugins/openhop.meshhub-bot/schema.json',
        ]),
        ('share/openhop/plugins/openhop.meshhub-bot/ui', [
            'openhop_meshhub_bot/share/openhop/plugins/openhop.meshhub-bot/ui/index.html',
            'openhop_meshhub_bot/share/openhop/plugins/openhop.meshhub-bot/ui/app.js',
            'openhop_meshhub_bot/share/openhop/plugins/openhop.meshhub-bot/ui/styles.css',
        ]),
    ]
)
