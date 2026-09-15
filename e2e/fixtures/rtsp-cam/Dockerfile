# The mediamtx image already carries ffmpeg (it is why the stack pins the
# -ffmpeg tag), so this fixture pulls no new encoder and uses the same build the
# relay itself spawns for a re-stamp hop.
#
# A FONT HAS TO BE INSTALLED, not vendored. The image ships none, so `drawtext`
# fails with "Cannot find a valid font for the family Sans" — and the burned-in
# timecode is the entire point of this fixture. It is installed here rather than
# committed because CONTRIBUTING.md's packaging check refuses any new binary in
# the tree, fonts named explicitly.
FROM bluenviron/mediamtx:1.9.1-ffmpeg@sha256:5bad7e4238f98e803f6af6bcc0bbe2bfb46db7d8c0af92c18909f88f27d8cf10
RUN apk add --no-cache font-dejavu python3 py3-pillow

# Pinned by path, not by family name: fontconfig's idea of "Sans" depends on
# what else is installed, and a fixture whose glyph metrics move is a fixture
# whose timecode crop moves with it.
ENV TIMECODE_FONT=/usr/share/fonts/dejavu/DejaVuSansMono.ttf

# The timecode decoder rides along in this image because it needs exactly two
# things the image already has: the SAME ffmpeg that drew the timecode, and the
# SAME font. Decoding elsewhere would mean keeping a second copy of both in
# step, which is the drift this avoids.
COPY timecode.py /timecode.py
COPY publish.sh /publish.sh
ENTRYPOINT ["/bin/sh", "/publish.sh"]
