import os

# publish-studio.ps1 tu tang so nay moi lan phat hanh
STUDIO_VERSION = '1.3.0'
# Bien moi truong chi dung de thu nghiem may chu cap nhat khac
UPDATE_URL = os.environ.get('YTDLP_STUDIO_UPDATE_URL') or 'https://ytdlp.luangroup.asia/latest.json'
