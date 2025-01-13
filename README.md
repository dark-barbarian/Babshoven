- `pip install -U "py-cord[voice]"`
- `pip install -U yt-dlp`
- `pip install -U bgutil-ytdlp-pot-provider`

Install ffmpeg, current directory is the virtual environment folder (e. g. /venv/)
- `wget "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"`
- `tar xf ffmpeg-master-latest-linux64-gpl.tar.xz`
- `rm ffmpeg-master-latest-linux64-gpl.tar.xz`
- `cd ffmpeg-master-latest-linux64-gpl/`
- `mv * ../`
- `mv bin/* ../bin/`
- `rm -r ffmpeg-master-latest-linux64-gpl/`

Set up POT provider
- `docker run --name bgutil-provider -d -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider`