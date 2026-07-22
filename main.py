import os
import time
import io
import json
import re
import logging
import sys
from PIL import Image
from google import genai
import requests

# 로깅 설정 (Coolify 실시간 로그 출력용 sys.stdout 지정)
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

# Verkada Video Tagging API 환경변수
VERKADA_ORG_ID = os.getenv("VERKADA_ORG_ID", "8c115ce0-a020-444b-ae3f-0b9d2352a592")
EVENT_TYPE_UID = os.getenv("EVENT_TYPE_UID", "3aebb77e-32ff-4ffd-bf21-5bc4c3c761fc")

# prompt.txt 절대 경로 설정 (main.py 위치 기준)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_FILE_PATH = os.getenv("PROMPT_FILE_PATH", os.path.join(BASE_DIR, "prompt.txt"))

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

    # 백업 프롬프트 (R1 ~ R5 대응)
    return """
    이 이미지에서 R1, R2, R3, R4, R5 위치의 경광등 색상을 분석해줘.
    반드시 [ {"label": "R1", "color": "GREEN"}, ... {"label": "R5", "color": "YELLOW"} ] 형태의 JSON 리스트로만 응답해.
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

            # 1. S3/CDN Pre-signed URL로 Redirect 되는 경우
            if response.status_code in [301, 302, 303, 307, 308]:
                s3_url = response.headers.get("Location")
                if s3_url:
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

    def send_video_tagging_event(self, camera_id, attributes_dict):
        """Verkada Video Tagging API로 경광등 상태 이벤트 전송"""
        token = self.get_token()
        if not token:
            logging.error("토큰이 없어 Helix 이벤트 전송을 스킵합니다.")
            return

        url = f"https://api.verkada.com/cameras/v1/video_tagging/event?org_id={VERKADA_ORG_ID}"
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "x-verkada-auth": token
        }

        payload = {
            "attributes": attributes_dict,
            "event_type_uid": EVENT_TYPE_UID,
            "camera_id": camera_id,
            "time_ms": int(time.time() * 1000)
        }

        # 디버깅 로그
        logging.info(f"[DEBUG] Helix 요청 URL: {url}")
        logging.info(f"[DEBUG] Helix 전송 Payload:\n{json.dumps(payload, indent=2, ensure_ascii=False)}")

        try:
            res = requests.post(url, headers=headers, json=payload, timeout=10)

            logging.info(f"[DEBUG] Helix 응답 상태 코드: {res.status_code}")
            logging.info(f"[DEBUG] Helix 응답 본문(Body): {res.text}")

            if res.status_code in [200, 201]:
                logging.info(f"Helix 이벤트 전송 성공 - 카메라: {camera_id}")
            else:
                logging.error(f"Helix 이벤트 전송 실패 [HTTP {res.status_code}]: {res.text}")

        except Exception as e:
            logging.error(f"Helix 이벤트 전송 중 예외 발생 ({camera_id}): {e}", exc_info=True)


def analyze_tower_light_with_gemini(image_bytes):
    """Gemini Vision API를 사용해 경광등 색상 분석 후 dict 구조로 변환"""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        prompt = load_gemini_prompt()

        response = ai_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[image, prompt]
        )

        raw_text = response.text.strip()

        # Gemini 마크다운 블록 제거 및 JSON 파싱
        cleaned_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.MULTILINE).strip()
        items = json.loads(cleaned_text)

        attributes_dict = {}
        if isinstance(items, list):
            for item in items:
                label = str(item.get("label", "")).strip().upper()
                color = str(item.get("color", "OFF")).strip().upper()
                if label:
                    attributes_dict[label] = color

        return attributes_dict

    except Exception as e:
        logging.error(f"Gemini 분석 또는 JSON 파싱 중 에러 발생: {e}")
        return None


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

                # 1. 썸네일 수집
                img_bytes = verkada.get_latest_thumbnail(camera_id)
                if not img_bytes:
                    logging.warning(f"카메라({camera_id}) 이미지를 가져오지 못해 스킵합니다.")
                    continue

                # 2. Gemini 분석 (JSON -> Dictionary)
                attributes_dict = analyze_tower_light_with_gemini(img_bytes)
                logging.info(f"카메라({camera_id}) 분석 결과: {attributes_dict}")

                # 3. STATUS 판별 로직 적용 (R1 ~ R5)
                if attributes_dict:
                    required_keys = ["R1", "R2", "R3", "R4", "R5"]
                    # R1부터 R5까지 모두 존재하고 GREEN인 경우 NORMAL, 하나라도 없거나 GREEN이 아니면 ABNORMAL
                    is_all_green = all(attributes_dict.get(k) == "GREEN" for k in required_keys)
                    attributes_dict["STATUS"] = "NORMAL" if is_all_green else "ABNORMAL"

                    # 4. Verkada Video Tagging API 전송
                    verkada.send_video_tagging_event(camera_id, attributes_dict)

        except Exception as e:
            logging.error(f"메인 루프 실행 중 에러 발생: {e}", exc_info=True)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
