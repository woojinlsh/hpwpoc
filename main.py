import os
import time
import io
import logging
from PIL import Image
from google import genai
import requests

# 로깅 설정
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# 환경변수 로드
VERKADA_API_KEY = os.getenv("VERKADA_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CAMERA_IDS = [cid.strip() for cid in os.getenv("CAMERA_IDS", "").split(",") if cid.strip()]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

ai_client = genai.Client(api_key=GEMINI_API_KEY)

class VerkadaClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.token = None
        self.token_expiry = 0
        
    def get_token(self):
        """Verkada API Token 발급 및 갱신"""
        if self.token and time.time() < self.token_expiry - 300:
            return self.token

        url = "https://api.verkada.com/token"
        headers = {
            "accept": "application/json",
            "x-api-key": self.api_key
        }
        
        try:
            response = requests.post(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            self.token = data.get("token")
            self.token_expiry = time.time() + 1800 
            logging.info("Verkada API 토큰 발급 성공")
            return self.token
        except Exception as e:
            logging.error(f"Verkada 토큰 발급 실패: {e}")
            return None

    def get_latest_thumbnail(self, camera_id):
        """카메라 최신 썸네일 이미지 다운로드 (S3 리다이렉트 403 우회 로직 적용)"""
        token = self.get_token()
        if not token:
            return None

        url = f"https://api.verkada.com/cameras/v1/devices/thumbnail/latest?camera_id={camera_id}"
        headers = {
            "x-verkada-auth": token
        }

        try:
            # allow_redirects=False로 설정하여 S3로 자동 이동할 때 인증 헤더가 유출되는 것 방지
            response = requests.get(url, headers=headers, allow_redirects=False)

            # Case 1: 301/302 Redirect 응답인 경우 (AWS S3 Pre-signed URL)
            if response.status_code in [301, 302, 303, 307, 308]:
                s3_url = response.headers.get("Location")
                if s3_url:
                    # S3 요청 시에는 Verkada 인증 헤더(x-verkada-auth)를 제거하고 순수 GET 요청
                    img_res = requests.get(s3_url)
                    img_res.raise_for_status()
                    return img_res.content

            # Case 2: JSON 타입으로 다운로드 URL을 반환하는 경우
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                data = response.json()
                img_url = data.get("url") or data.get("thumbnail_url")
                if img_url:
                    img_res = requests.get(img_url)
                    img_res.raise_for_status()
                    return img_res.content

            # Case 3: 바로 바이너리 이미지가 반환되는 경우
            response.raise_for_status()
            return response.content

        except Exception as e:
            logging.error(f"카메라({camera_id}) 썸네일 수집 실패: {e}")
            return None

    def send_helix_event(self, camera_id, light_color):
        """Verkada Helix로 판별된 경광등 상태 이벤트 전송"""
        token = self.get_token()
        if not token:
            return

        url = "https://api.verkada.com/helix/v1/events"
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "x-verkada-auth": token
        }
        
        payload = {
            "camera_id": camera_id,
            "event_type": "tower_light_status",
            "event_time": int(time.time() * 1000),
            "attributes": {
                "light_color": light_color,
                "status": "NORMAL" if light_color == "GREEN" else "WARNING/STOP"
            }
        }

        try:
            res = requests.post(url, headers=headers, json=payload)
            res.raise_for_status()
            logging.info(f"Helix 이벤트 전송 성공 - 카메라: {camera_id}, 상태: {light_color}")
        except Exception as e:
            logging.error(f"Helix 이벤트 전송 실패 ({camera_id}): {e}")


def analyze_tower_light_with_gemini(image_bytes):
    """Gemini API를 사용해 경광등 색상 판별"""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        
        prompt = """
        이 이미지에 있는 장비의 경광등(Tower Light/Signal Tower) 색상을 분석해줘.
        다음 목록 중 가장 알맞은 상태 하나만 반드시 대문자로 출력해:
        - RED (빨간색 점등)
        - YELLOW (노란색/주황색 점등)
        - GREEN (초록색 점등)
        - OFF (꺼짐 또는 경광등을 찾을 수 없음)
        
        응답은 다른 설명 없이 오직 위의 단어 중 하나만 반환해 (예: GREEN).
        """
        
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[image, prompt]
        )
        
        result = response.text.strip().upper()
        if result not in ["RED", "YELLOW", "GREEN", "OFF"]:
            logging.warning(f"Gemini 응답 미인식: {result}, 기본값 OFF 처리")
            return "OFF"
            
        return result
    except Exception as e:
        logging.error(f"Gemini 분석 중 에러 발생: {e}")
        return "UNKNOWN"


def main():
    verkada = VerkadaClient(api_key=VERKADA_API_KEY)
    logging.info("경광등 감시 모니터링 시스템을 시작합니다.")
    
    while True:
        for camera_id in CAMERA_IDS:
            logging.info(f"카메라({camera_id}) 상태 점검 중...")
            
            img_bytes = verkada.get_latest_thumbnail(camera_id)
            if not img_bytes:
                continue
            
            color_status = analyze_tower_light_with_gemini(img_bytes)
            logging.info(f"카메라({camera_id}) 감지 결과: {color_status}")
            
            if color_status != "UNKNOWN":
                verkada.send_helix_event(camera_id, color_status)
                
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
