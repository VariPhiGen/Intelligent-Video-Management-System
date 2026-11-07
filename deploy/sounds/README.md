Sound files for the device gateway's multicast speakers.

Put `.wav` files here (any rate or channel count — they are converted to the
8 kHz mono u-law paging speakers expect). A rule or the device wall can then
name one (`"sound": "stay_away.wav"`); `tone` is a built-in two-pitch siren and
needs no file. Mounted read-only into vms_device_gateway at /sounds; point
GATEWAY_SOUNDS_HOST_DIR elsewhere to keep sounds outside the repo.
