#!/bin/sh
# publish.sh — a synthetic camera: an RTSP SERVER that serves a timecoded stream.
#
# THE SHAPE MATTERS, AND IT IS EASY TO GET BACKWARDS. A camera is a server. The
# VMS does not receive a stream from it; the VMS creates a MediaMTX path whose
# `source` is the camera's own RTSP URL, and MediaMTX PULLS. This fixture was
# first written as a publisher pushing into the stack's relay, which fails with
# "path 'e2e-cam-1' is not configured" — correctly, because the product's
# MediaMTX has no catch-all path and only ever holds paths the api created.
#
# So the container runs its own MediaMTX as the camera's RTSP server, with
# ffmpeg feeding it locally. The base image carries both, which is why this
# needs nothing extra.
#
# WHY A BURNED-IN TIMECODE. Every other way of checking that playback landed on
# the right second is a judgement call: a screenshot someone has to read, or a
# duration that drifts. A frame carrying its own wall-clock time as pixels turns
# "did the scrub land where we asked" into an equality assertion.
#
# TWO STAMPS, AND THE FORMATS ARE CHOSEN FOR THE ESCAPING.
#   EPOCH <n>   what the tests parse: one integer, monospace, high contrast on a
#               solid band, so reading it back needs no cleverness.
#   HH-MM-SS    for whoever is looking at a failure screenshot. DASHES, not
#               colons, because a colon inside a drawtext format needs escaping
#               and the readable stamp is not worth that fight.
#
# THE TEXT VALUE IS SINGLE-QUOTED INSIDE THE FILTER STRING. Escaping the colon
# as `\:` does NOT survive: drawtext receives `%{localtime` and warns
# "Unterminated %{}". ffmpeg treats that as a warning and still exits 0, so the
# stream publishes with a frozen literal timecode and a test asserting on it
# would compare against a constant and pass. Anything changing this must check
# ffmpeg's stderr for "Unterminated", not its exit status.
#
# ALSO DELIBERATE:
#   -re         serve at wall-clock rate, so one second of stream is one second
#               of time; without it the recorded timeline means nothing.
#   fixed GOP   a keyframe every second, so a scrub can land anywhere.
#   no audio    nothing under test consumes it.
set -eu

CAM_NAME="${CAM_NAME:?CAM_NAME is required}"
CAM_PROFILE="${CAM_PROFILE:-16x9}"
CAM_FPS="${CAM_FPS:-15}"
CAM_RTSP_PORT="${CAM_RTSP_PORT:-8554}"
FONT="${TIMECODE_FONT:-/usr/share/fonts/dejavu/DejaVuSansMono.ttf}"

case "$CAM_PROFILE" in
  16x9) SIZE=1280x720 ;;
  4x3)  SIZE=1024x768 ;;
  sub)  SIZE=640x360  ;;
  *)    echo "unknown CAM_PROFILE=$CAM_PROFILE" >&2; exit 2 ;;
esac

# A different hue per camera, so a wall of tiles is distinguishable in a failure
# screenshot without reading any text.
case "$CAM_NAME" in
  *1*) HUE=0x1E5C7B ;;
  *2*) HUE=0x7B3A1E ;;
  *)   HUE=0x1E7B4A ;;
esac

BAND='drawbox=x=0:y=0:w=iw:h=150:color=black:t=fill'
CLOCK="drawtext=fontfile=${FONT}:text='%{localtime\\:%Y-%m-%d %H-%M-%S}':x=24:y=16:fontsize=32:fontcolor=white"
EPOCH="drawtext=fontfile=${FONT}:text='EPOCH %{localtime\\:%s}':x=24:y=64:fontsize=54:fontcolor=0x00FF88"
LABEL="drawtext=fontfile=${FONT}:text='${CAM_NAME}':x=24:y=h-52:fontsize=30:fontcolor=white"
VF="${BAND},${CLOCK},${EPOCH},${LABEL}"

# A SECOND, LOW-RESOLUTION PATH — because a real camera has one.
#
# The ONVIF simulator advertises two profiles (MainStream and SubStream), and
# `substream.candidate_urls` probes the second one when resolving a sub track.
# A fixture that advertised a sub and did not serve it would make every
# sub-track path in the product untestable: the probe would fail, no sub would
# ever be resolved, and the two-track teardown contract would have nothing to
# tear down. The sub is the same picture and the same timecode at 640x360, so
# it is distinguishable from the main by shape alone.
SUB_NAME="${CAM_NAME}-sub"
SUB_SIZE=640x360

cat > /camera.yml <<YML
logLevel: info
rtspAddress: 0.0.0.0:${CAM_RTSP_PORT}
rtmp: no
hls: no
webrtc: no
srt: no
api: no
paths:
  ${CAM_NAME}:
    runOnInit: >
      ffmpeg -hide_banner -loglevel warning -re
      -f lavfi -i color=c=${HUE}:s=${SIZE}:r=${CAM_FPS}
      -vf "${VF}"
      -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p
      -g ${CAM_FPS} -keyint_min ${CAM_FPS} -sc_threshold 0 -an
      -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:${CAM_RTSP_PORT}/${CAM_NAME}
    runOnInitRestart: yes
  ${SUB_NAME}:
    runOnInit: >
      ffmpeg -hide_banner -loglevel warning -re
      -f lavfi -i color=c=${HUE}:s=${SUB_SIZE}:r=${CAM_FPS}
      -vf "${VF}"
      -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p
      -g ${CAM_FPS} -keyint_min ${CAM_FPS} -sc_threshold 0 -an
      -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:${CAM_RTSP_PORT}/${SUB_NAME}
    runOnInitRestart: yes
YML

echo "[rtsp-cam] ${CAM_NAME}  ${SIZE} @ ${CAM_FPS}fps  serving rtsp://0.0.0.0:${CAM_RTSP_PORT}/${CAM_NAME}"
exec /mediamtx /camera.yml
