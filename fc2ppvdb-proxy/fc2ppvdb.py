import sys
import json
import requests
from py_common import log
from py_common.util import scraper_args

def test_connection():
    """Test connection to Flask server"""
    log.debug("Testing connection to fc2ppvdb-proxy server...")
    url = "http://9.9.9.124:5000/test"

    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise exception for bad status codes

        result = {
            "success": True,
            "status_code": response.status_code,
            "response_text": response.text,
            "response_json": None
        }

        # Try to parse as JSON if possible
        try:
            result["response_json"] = response.json()
        except json.JSONDecodeError:
            pass

        return result

    except requests.exceptions.RequestException as e:
        return {
            "success": False,
            "error": str(e),
            "status_code": None,
            "response_text": None,
            "response_json": None
        }


if __name__ == "__main__":
    log.debug("Starting fc2ppvdb-proxy test connection")
    res = test_connection_result = test_connection()
    log.debug(f"Test connection result: {res}")
    # op, args = scraper_args()
    # result = None
    # match op, args:
    #     case "performer-by-name", {"name": name} if name:
    #         result = performer_by_name(name)
    #     case _:
    #       log.error(f"Operation: {op}, arguments: {json.dumps(args)}")
    #       sys.exit(1)

    print(json.dumps(res))