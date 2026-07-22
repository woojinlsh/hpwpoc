import os
import time
import io
import logging
import sys
from PIL import Image
from google import genai
import requests

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

# 환경변수 로드
VERKADA_API_KEY = os.getenv("VERKADA_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CAMERA_IDS = [cid.strip() for cid in os.getenv("CAMERA_IDS", "").split(",") if cid.strip()]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
PROMPT_FILE_PATH = os.getenv("PROMPT_FILE_PATH", "prompt.txt")

# Gemini 클라이언트 초기화
ai_client = genai.Client(api_key=GEMINI_API_KEY)


def load_gemini_prompt():
    """prompt.txt 파일에서 프롬프트 텍스트를 읽어옵니다."""
    try:
        if os.path.exists(PROMPT_FILE_PATH):
            with open(PROMPT_FILE_PATH, "r", encoding="utf-8") as f:
                prompt_text = f.read().strip()
                if prompt_text:
                    return prompt_text
        logging.warning(f"'{PROMPT_FILE_PATH}' 파일이 없거나 비어있어 기본 내장 프롬프트를 사용합니다.")
    except Exception as e:
        logging.error(f"프롬프트 파일 읽기 오류: {e}")

    # 파일 읽기 실패 시 사용할 백업 프롬프트
    return """
    이 이미지에 있는 장비의 경광등(Tower Light/Signal Tower) 색상을 분석해줘.
    다음 목록 중 가장 알맞은 상태 하나만 반드시 대문자로 출력해:
    - RED (빨간색 점등)
    - YELLOW (노란색/주황색 점등)
    - GREEN (초록색 점등)
    - OFF (꺼짐 또는 경광등을 찾을 수 없음)

    응답은 다른 설명 없이 오직 위의 단어 중 하나만 반환해 (예: GREEN).
    """


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
            response = requests.post(url, headers=headers, timeout=10)
            if response.status_code != 200:
                logging.error(f"토큰 발급 실패 [HTTP {response.status_code}]: {response.text}")
                return None

            data = response.json()
            self.token = data.get("token")
            self.token_expiry = time.time() + 1800
            logging.info("Verkada API 토큰 발급 성공")
            return self.token
        except Exception as e:
            logging.error(f"Verkada 토큰 요청 중 예외 발생: {e}")
            return None

    def get_latest_thumbnail(self, camera_id):
        """카메라 최신 썸네일 이미지 다운로드"""
        token = self.get_token()
        if not token:
            logging.error("토큰이 없어 썸네일 요청을 중단합니다.")
            return None

        url = f"https://api.verkada.com/cameras/v1/footage/thumbnails/latest?camera_id={camera_id}"
        headers = {
            "x-verkada-auth": token,
            "accept": "image/jpeg, application/json"
        }

        try:
            logging.info(f"Verkada 썸네일 API 호출 중... (Camera ID: {camera_id})")
            response = requests.get(url, headers=headers, allow_redirects=False, timeout=15)
            logging.info(f"Verkada API 응답 상태 코드: {response.status_code}")

            # 1. S3/CDN Pre-signed URL로 Redirect 되는 경우
            if response.status_code in [301, 302, 303, 307, 308]:
                s3_url = response.headers.get("Location")
                if s3_url:
                    logging.info("Redirect 감지! S3 보안 URL에서 이미지 다운로드를 진행합니다.")
                    img_res = requests.get(s3_url, timeout=15)
                    if img_res.status_code == 200:
                        return img_res.content
                    else:
                        logging.error(f"S3 이미지 다운로드 실패 [HTTP {img_res.status_code}]: {img_res.text}")
                        return None

            # 2. JSON 형태로 URL을 반환하는 경우
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                try:
                    data = response.json()
                    img_url = data.get("url") or data.get("thumbnail_url")
                    if img_url:
                        img_res = requests.get(img_url, timeout=15)
                        return img_res.content
                except Exception as json_err:
                    logging.error(f"JSON 파싱 실패: {json_err}")

            # 3. 직접 바이너리 이미지를 반환하는 경우
            if response.status_code == 200:
                return response.content

            logging.error(f"썸네일 API 에러 응답 [HTTP {response.status_code}]: {response.text}")
            return None

        except requests.exceptions.Timeout:
            logging.error(f"카메라({camera_id}) 썸네일 요청 시간 초과 (Timeout)")
            return None
        except Exception as e:
            logging.error(f"카메라({camera_id}) 썸네일 수집 중 예외 발생: {e}", exc_info=True)
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
            res = requests.post(url, headers=headers, json=payload, timeout=10)
            if res.status_code in [200, 201]:
                logging.info(f"Helix 이벤트 전송 성공 - 카메라: {camera_id}, 상태: {light_color}")
            else:
                logging.error(f"Helix 이벤트 전송 실패 [HTTP {res.status_code}]: {res.text}")
        except Exception as e:
            logging.error(f"Helix 이벤트 전송 중 예외 발생 ({camera_id}): {e}")


def analyze_tower_light_with_gemini(image_bytes):
    """Gemini Vision API를 사용해 경광등 색상 판별"""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        
        # 외부 prompt.txt 파일에서 프롬프트 불러오기
        prompt = load_gemini_prompt()

        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[image, prompt]
        )

        result = response.text.strip().upper()
        if result not in ["RED", "YELLOW", "GREEN", "OFF"]:
            logging.warning(f"Gemini 응답 미인식({result}), 기본값 OFF 처리")
            return "OFF"

        return result
    except Exception as e:
        logging.error(f"Gemini 분석 중 에러 발생: {e}")
        return "UNKNOWN"


def main():
    if not VERKADA_API_KEY or not GEMINI_API_KEY or not CAMERA_IDS:
        logging.error("필수 환경변수(VERKADA_API_KEY, GEMINI_API_KEY, CAMERA_IDS)가 설정되지 않았습니다.")
        return

    verkada = VerkadaClient(api_key=VERKADA_API_KEY)
    logging.info("경광등 감시 모니터링 시스템을 시작합니다.")

    while True:
        try:
            for camera_id in CAMERA_IDS:
                logging.info(f"카메라({camera_id}) 상태 점검 중...")

                img_bytes = verkada.get_latest_thumbnail(camera_id)
                if not img_bytes:
                    logging.warning(f"카메라({camera_id}) 이미지를 가져오지 못해 스킵합니다.")
                    continue

                color_status = analyze_tower_light_with_gemini(img_bytes)
                logging.info(f"카메라({camera_id}) 분석 결과: {color_status}")

                if color_status != "UNKNOWN":
                    verkada.send_helix_event(camera_id, color_status)

        except Exception as e:
            logging.error(f"메인 루프 실행 중 에러 발생: {e}", exc_info=True)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
