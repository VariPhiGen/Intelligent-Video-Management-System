"""VMS device gateway — the VMS's Webhook driver calls this; this speaks to devices.

    python gateway.py            # GATEWAY_BIND (127.0.0.1), GATEWAY_PORT (8099)

See devgw/server.py for the URL contract and README.md for setup.
"""
import logging
import os

from devgw.server import Gateway, Settings, serve

if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "info").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    host = os.environ.get("GATEWAY_BIND", "127.0.0.1")
    port = int(os.environ.get("GATEWAY_PORT", "8099"))
    httpd = serve(host, port, Gateway(settings))
    logging.info("device gateway on http://%s:%d (sounds=%s, multicast_if=%s, relay_max_on=%ss)",
                 host, port, settings.sounds_dir, settings.multicast_if or "auto",
                 settings.relay_max_on_s)
    httpd.serve_forever()
