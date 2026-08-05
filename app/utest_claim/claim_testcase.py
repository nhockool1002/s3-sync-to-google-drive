"""
Trích dữ liệu uTest test case bằng Selenium.

Chrome giữ mở sau mỗi testcase để tiếp tục URL khác mà không cần đăng nhập lại.

Chạy:
  python -m app.utest_claim.claim_testcase
  python -m app.utest_claim.claim_testcase --url "https://platform.utest.com/testcycles/549790/testcases/11947523"
"""

from __future__ import annotations

import argparse
import html as html_module
import json
import re
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.webdriver import WebDriver

from app.utils import ensure_dir
from app.utest_claim.selenium_browser import create_chrome_driver, navigate

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_ROOT = PROJECT_ROOT / "utest_claim_testcase"

TESTCASE_ID_RE = re.compile(r"/testcases/(\d+)")
ATTACHMENT_RE = re.compile(r'\[attachment\]="(\{[^"]+\})"')
STEP_TITLE_RE = re.compile(r"issue-title[^>]*>\s*Step\s+(\d+)", re.IGNORECASE)
SURVEY_QUESTION_RE = re.compile(
    r'<dd class="step-question">\s*'
    r'<div ng-bind-html="[^"]+">(.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)
SURVEY_ANSWER_RE = re.compile(
    r'class="step-question-options".*?'
    r'ng-bind-html="[^"]+">(.*?)</span>',
    re.DOTALL | re.IGNORECASE,
)


@dataclass(frozen=True)
class Attachment:
    attachment_id: int
    storage_url: str
    file_name: str

    @classmethod
    def from_raw_json(cls, raw: str) -> Attachment | None:
        try:
            data = json.loads(html_module.unescape(raw))
        except (json.JSONDecodeError, TypeError):
            return None
        attachment_id = data.get("id")
        storage_url = data.get("storageUrl") or data.get("storage_url")
        file_name = data.get("fileName") or data.get("file_name")
        if not attachment_id or not storage_url or not file_name:
            return None
        return cls(
            attachment_id=int(attachment_id),
            storage_url=str(storage_url),
            file_name=str(file_name).strip(),
        )


def extract_testcase_id(url: str) -> str:
    match = TESTCASE_ID_RE.search(url)
    if not match:
        raise ValueError(
            "URL phải chứa /testcases/<ID>, "
            "ví dụ: https://platform.utest.com/testcycles/549790/testcases/11947523"
        )
    return match.group(1)


def _validate_utest_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("URL không được để trống.")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL phải bắt đầu bằng http:// hoặc https://")
    if "utest.com" not in (parsed.netloc or ""):
        raise ValueError("URL phải thuộc domain utest.com.")
    return url


def _split_step_sections(page_html: str) -> tuple[str, str]:
    matches = list(STEP_TITLE_RE.finditer(page_html))
    if len(matches) < 2:
        raise ValueError(
            "Không tìm thấy đủ Step 1 và Step 2 trong HTML. "
            "Hãy mở tab Description và đợi trang render xong."
        )

    step1_start = None
    step2_start = None
    for match in matches:
        step_no = int(match.group(1))
        if step_no == 1 and step1_start is None:
            step1_start = match.start()
        elif step_no == 2 and step2_start is None:
            step2_start = match.start()

    if step1_start is None or step2_start is None or step2_start <= step1_start:
        raise ValueError("Không xác định được vùng Step 1 / Step 2 trong HTML.")

    return page_html[step1_start:step2_start], page_html[step2_start:]


def parse_step1_attachments(page_html: str) -> list[Attachment]:
    step1_html, _ = _split_step_sections(page_html)
    seen_ids: set[int] = set()
    attachments: list[Attachment] = []

    for raw in ATTACHMENT_RE.findall(step1_html):
        item = Attachment.from_raw_json(raw)
        if item is None or item.attachment_id in seen_ids:
            continue
        seen_ids.add(item.attachment_id)
        attachments.append(item)

    return attachments


def _clean_text(text: str) -> str:
    cleaned = html_module.unescape(text)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def parse_step2_feedback(page_html: str) -> list[tuple[str, str]]:
    _, step2_html = _split_step_sections(page_html)
    questions = [_clean_text(q) for q in SURVEY_QUESTION_RE.findall(step2_html)]
    answers = [_clean_text(a) for a in SURVEY_ANSWER_RE.findall(step2_html)]

    if not questions:
        raise ValueError("Không tìm thấy câu hỏi UX Feedback (Step 2) trong HTML.")

    if len(answers) < len(questions):
        raise ValueError(
            f"Tìm thấy {len(questions)} câu hỏi nhưng chỉ có {len(answers)} câu trả lời."
        )

    return list(zip(questions, answers[: len(questions)], strict=False))


def _safe_filename(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', "_", name).strip()
    return safe or "attachment"


def _build_requests_session(cookies: list[dict[str, Any]]) -> requests.Session:
    session = requests.Session()
    for cookie in cookies:
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def download_attachments(
    attachments: list[Attachment],
    cookies: list[dict[str, Any]],
    output_dir: Path,
) -> list[Path]:
    ensure_dir(str(output_dir))
    session = _build_requests_session(cookies)
    saved: list[Path] = []

    for index, attachment in enumerate(attachments, start=1):
        file_name = _safe_filename(attachment.file_name)
        dest = output_dir / file_name
        if dest.exists():
            dest = output_dir / f"{dest.stem}_{attachment.attachment_id}{dest.suffix}"

        print(f"  [{index}/{len(attachments)}] Tải {file_name} ...")
        response = session.get(attachment.storage_url, timeout=120)
        response.raise_for_status()
        dest.write_bytes(response.content)
        saved.append(dest)

    return saved


def format_feedback_text(
    testcase_id: str,
    url: str,
    feedback_items: list[tuple[str, str]],
) -> str:
    lines = [
        "=== UX Feedback (Step 2) ===",
        f"Test Case ID: {testcase_id}",
        f"URL: {url}",
        "",
    ]
    for index, (question, answer) in enumerate(feedback_items, start=1):
        lines.extend(
            [
                f"--- Question {index} ---",
                question,
                "",
                f"Response: {answer}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _prompt_yes_no(message: str) -> bool:
    while True:
        answer = input(f"{message} (y/n): ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Vui lòng nhập y hoặc n.")


def claim_testcase(
    driver: WebDriver,
    url: str,
    output_root: Path | None = None,
) -> Path:
    url = _validate_utest_url(url)
    testcase_id = extract_testcase_id(url)
    root = output_root or OUTPUT_ROOT
    case_dir = root / testcase_id
    step1_dir = case_dir / "Step1"
    step2_dir = case_dir / "Step2"
    feedback_path = step2_dir / "Feedback.txt"

    print(f"\nTest Case ID: {testcase_id}")
    print(f"Thư mục đích: {case_dir}")
    print("\nĐang mở URL trong Chrome (Selenium) ...")
    navigate(driver, url)

    print("\nChrome đã mở trang uTest.")
    print("- Đăng nhập nếu cần (chỉ lần đầu; session được lưu trong profile).")
    print("- Mở tab Description và đợi Step 1 / Step 2 hiển thị đầy đủ.")
    print("- Cuộn hết trang để ảnh đính kèm được render.")

    if not _prompt_yes_no("\nĐã lấy được toàn bộ HTML chưa?"):
        print("Đã bỏ qua testcase này.")
        return case_dir

    page_html = driver.page_source
    cookies = driver.get_cookies()

    print("Đang phân tích HTML ...")
    attachments = parse_step1_attachments(page_html)
    feedback_items = parse_step2_feedback(page_html)

    print(f"\nStep 1: tìm thấy {len(attachments)} ảnh đính kèm.")
    if attachments:
        print("Đang tải ảnh vào Step1/ ...")
        for path in download_attachments(attachments, cookies, step1_dir):
            print(f"  OK  {path}")
    else:
        ensure_dir(str(step1_dir))
        print("  (không có ảnh)")

    ensure_dir(str(step2_dir))
    feedback_path.write_text(
        format_feedback_text(testcase_id, url, feedback_items),
        encoding="utf-8",
    )
    print(f"\nStep 2: đã ghi {len(feedback_items)} câu hỏi/trả lời vào {feedback_path}")
    print(f"\nHoàn tất testcase {testcase_id}: {case_dir}")
    return case_dir


def run_claim_loop(
    output_root: Path,
    *,
    first_url: str | None = None,
) -> None:
    driver: WebDriver | None = None
    try:
        print("Đang khởi động Chrome (Selenium) ...")
        driver = create_chrome_driver()
        print("Chrome đã mở. Profile lưu tại utest_claim_testcase/.chrome_profile")

        url = first_url
        while True:
            if not url:
                url = input(
                    "\nNhập URL test case uTest tiếp theo (Enter để thoát): "
                ).strip()
                if not url:
                    break

            try:
                claim_testcase(driver, url, output_root=output_root)
            except (ValueError, requests.RequestException, WebDriverException) as exc:
                print(f"\nLỗi với URL {url}: {exc}", file=sys.stderr)
                traceback.print_exc()
            except Exception:  # noqa: BLE001
                print(f"\nLỗi không mong đợi với URL {url}:", file=sys.stderr)
                traceback.print_exc()

            print("\nChrome vẫn mở — bạn có thể tiếp tục URL khác.")
            url = None

    finally:
        if driver is not None:
            if _prompt_yes_no("\nĐóng Chrome?"):
                driver.quit()
            else:
                print("Chrome vẫn mở. Bạn có thể đóng thủ công.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Selenium: trích Step1 ảnh + Step2 feedback từ uTest, giữ Chrome mở giữa các testcase."
    )
    parser.add_argument(
        "--url",
        help="URL test case đầu tiên (sau đó có thể nhập thêm URL khác).",
    )
    parser.add_argument(
        "--output-dir",
        default=str(OUTPUT_ROOT),
        help=f"Thư mục gốc output (mặc định: {OUTPUT_ROOT})",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    output_root = Path(args.output_dir).expanduser().resolve()
    first_url = args.url
    if first_url is None:
        entered = input("Nhập URL test case uTest (Enter để bỏ qua và nhập sau): ").strip()
        first_url = entered or None

    try:
        run_claim_loop(output_root, first_url=first_url)
    except WebDriverException as exc:
        print(f"\nKhông khởi động được Chrome/Selenium: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
