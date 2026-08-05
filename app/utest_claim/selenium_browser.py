"""Quản lý Chrome Selenium với profile lưu đăng nhập uTest."""

from __future__ import annotations

import os
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PROFILE_DIR = PROJECT_ROOT / "utest_claim_testcase" / ".chrome_profile"
DOCUMENT_READY_TIMEOUT = 60


def chrome_profile_dir() -> Path:
    custom = os.getenv("UTEST_CHROME_PROFILE_DIR")
    if custom:
        return Path(custom).expanduser().resolve()
    return DEFAULT_PROFILE_DIR


def create_chrome_driver(*, profile_dir: Path | None = None) -> WebDriver:
    profile = profile_dir or chrome_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_argument(f"--user-data-dir={profile}")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    return webdriver.Chrome(options=options)


def wait_for_document_ready(driver: WebDriver, timeout: int = DOCUMENT_READY_TIMEOUT) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def navigate(driver: WebDriver, url: str) -> None:
    driver.get(url)
    wait_for_document_ready(driver)
