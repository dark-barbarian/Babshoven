- `pip install -U "py-cord[voice]"`
- `pip install -U yt-dlp`

Install ffmpeg, current directory is the virtual environment folder (e. g. /venv/)
- `wget "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"`
- `tar xf ffmpeg-master-latest-linux64-gpl.tar.xz`
- `rm ffmpeg-master-latest-linux64-gpl.tar.xz`
- `cd ffmpeg-master-latest-linux64-gpl/`
- `mv * ../`
- `mv bin/* ../bin/`
- `rm -r ffmpeg-master-latest-linux64-gpl/`
