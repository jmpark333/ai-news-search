#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 뉴스 검색 및 블로그 생성 시스템
최신 AI 기사 검색, 주제 추천, 블로그 자동 작성을 웹에서 제공
"""

import os
import sys
import json
import datetime
import requests
import sqlite3
import time
import re
import webbrowser
import tempfile
import threading
import logging
import hashlib
import queue
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify, send_file, g, Response
from flask_cors import CORS
from contextlib import contextmanager
import urllib.parse

# 로깅 설정 (로그 회전 포함)
import logging.handlers

# 현재 작업 디렉토리 확인
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 로그 회전 핸들러 설정 - 최대 10MB, 백업 5개 유지
log_file = os.path.join(BASE_DIR, "ai_blog_system.log")
file_handler = logging.handlers.RotatingFileHandler(
    log_file,
    maxBytes=10 * 1024 * 1024,  # 10MB
    backupCount=5,  # 백업 파일 5개 유지
    encoding="utf-8",
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)

# Flask 서버 로그를 위한 별도 회전 핸들러
flask_log_file = os.path.join(BASE_DIR, "flask_server.log")
flask_handler = logging.handlers.RotatingFileHandler(
    flask_log_file,
    maxBytes=10 * 1024 * 1024,  # 10MB
    backupCount=5,  # 백업 파일 5개 유지
    encoding="utf-8",
)
flask_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)

# 기본 로거 설정
logging.basicConfig(
    level=logging.DEBUG,  # DEBUG 레벨로 변경하여 상세한 디버깅 정보 표시
    handlers=[file_handler, logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# Flask 로거 설정
flask_logger = logging.getLogger("werkzeug")
flask_logger.addHandler(flask_handler)
flask_logger.setLevel(logging.WARNING)  # INFO에서 WARNING으로 변경하여 과도한 로그 감소

# watchdog 로거 레벨 설정 (디버그 메시지 제거)
watchdog_logger = logging.getLogger("watchdog")
watchdog_logger.setLevel(logging.WARNING)  # DEBUG에서 WARNING으로 변경

# 다른 과도한 로거들도 레벨 조정
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

# 설정
DB_FILE = os.path.join(BASE_DIR, "enhanced_ai_blog_database.db")
NAVER_CLIENT_ID = "eaxPqk5yaWE9N4tVgRA8"
NAVER_CLIENT_SECRET = "HF4MYYE3qs"
# Tavily Search API 설정
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
# Z.AI API 설정 (비활성화)
# ZAI_API_KEY = "6e74659313a8456da1b4881d29dc098f.SgJrKDIG5qoTW9YO"
# ZAI_API_URL = "https://api.z.ai/api/paas/v4/chat/completions"

# 성능 설정 (네이버 API 안정성 강화)
MAX_WORKERS = 2  # 워커 수 줄임으로 시스템 부하 감소
REQUEST_TIMEOUT = 45  # 타임아웃 증가
RATE_LIMIT_DELAY = 1.0  # 네이버 API 요청 간격 증가
CACHE_TTL = 3600  # 1시간

# 검색 키워드 목록
SEARCH_KEYWORDS = [
    "대규모 언어모델",
    "OpenAI ChatGPT",
    "Anthropic Claude",
    "Google Gemini Pro",
    "DeepSeek Qwen Alibaba",
    "Meta xAI Grok",
    "NVIDIA Microsoft",
    "KT SK LG AI",
    "LLM 파인튜닝",
    "생성형 AI 챗봇",
    "AI 컴퓨팅 AI 클라우드",
    "AI 로봇 자율주행",
    "AI 의료 AI 교육 AI 금융",
    "AI 코딩 AI 디자인 AI 콘텐츠 생성",
    "AR VR 스마트 기기",
    "AI 기술 AI 보안 AI 규제",
    "GLM AI",
    "Github",
    "코파일럿",
]

app = Flask(__name__)
CORS(app)


@dataclass
class SearchResult:
    """검색 결과 데이터 클래스"""

    title: str
    summary: str
    url: str
    source: str
    publish_date: str
    keywords: List[str]
    relevance_score: float = 0.0
    language: str = "ko"


@dataclass
class ExcludedSite:
    """제외된 사이트 데이터 클래스"""

    id: int
    domain: str
    url_pattern: Optional[str]
    reason: str
    created_at: str
    active: bool


@dataclass
class ExcludedPage:
    """제외된 페이지 데이터 클래스"""

    id: int
    url: str
    title: Optional[str]
    reason: str
    created_at: str
    active: bool


class DatabaseManager:
    """데이터베이스 관리 클래스"""

    def __init__(self, db_file: str = DB_FILE):
        self.db_file = db_file
        self._lock = threading.RLock()

    @contextmanager
    def get_connection(self):
        """데이터베이스 연결 컨텍스트 매니저"""
        with self._lock:
            conn = None
            try:
                # 타임아웃 설정 추가 (30초)
                conn = sqlite3.connect(
                    self.db_file, check_same_thread=False, timeout=30.0
                )
                conn.row_factory = sqlite3.Row
                yield conn
            except Exception as e:
                logger.error(f"데이터베이스 연결 오류: {str(e)}")
                raise
            finally:
                if conn:
                    conn.close()

    def initialize_database(self) -> bool:
        """데이터베이스 초기화"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # 버전 관리 테이블
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS schema_version (
                        version INTEGER PRIMARY KEY,
                        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # 기사 저장 테이블
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS articles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        summary TEXT,
                        url TEXT NOT NULL UNIQUE,
                        source TEXT,
                        publish_date TEXT,
                        relevance_score REAL DEFAULT 0.0,
                        language TEXT DEFAULT 'ko',
                        keywords TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # 제외된 사이트 테이블
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS excluded_sites (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        domain TEXT NOT NULL UNIQUE,
                        url_pattern TEXT,
                        reason TEXT DEFAULT 'user_excluded',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        active BOOLEAN DEFAULT 1
                    )
                """)

                # 제외된 페이지 테이블
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS excluded_pages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL UNIQUE,
                        title TEXT,
                        reason TEXT DEFAULT 'user_excluded',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        active BOOLEAN DEFAULT 1
                    )
                """)

                # 검색 캐시 테이블
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS search_cache (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        query_hash TEXT NOT NULL UNIQUE,
                        query TEXT,
                        results TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP
                    )
                """)

                # 인덱스 생성
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_articles_publish_date ON articles(publish_date)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_excluded_sites_domain ON excluded_sites(domain)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_excluded_pages_url ON excluded_pages(url)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_search_cache_query_hash ON search_cache(query_hash)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_search_cache_expires_at ON search_cache(expires_at)"
                )

                # 버전 관리 및 마이그레이션
                self._run_migrations(cursor)

                conn.commit()
                logger.info("데이터베이스 초기화 완료")
                return True

        except Exception as e:
            logger.error(f"데이터베이스 초기화 실패: {str(e)}")
            return False

    def _run_migrations(self, cursor):
        """데이터베이스 마이그레이션 실행"""
        # 현재 버전 확인
        cursor.execute("SELECT MAX(version) FROM schema_version")
        current_version = cursor.fetchone()[0] or 0

        # 마이그레이션 목록 - 이미 적용된 마이그레이션은 제외
        migrations = [
            (1, "ALTER TABLE articles ADD COLUMN language TEXT DEFAULT 'ko'"),
            (2, "ALTER TABLE articles ADD COLUMN keywords TEXT"),
            (
                3,
                "CREATE TABLE search_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, query_hash TEXT NOT NULL UNIQUE, query TEXT, results TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMP)",
            ),
        ]

        for version, migration_sql in migrations:
            if current_version < version:
                try:
                    # 테이블/컬럼 존재 여부 확인 후 마이그레이션 실행
                    if version == 1:
                        # language 컬럼이 이미 있는지 확인
                        cursor.execute("PRAGMA table_info(articles)")
                        columns = [column[1] for column in cursor.fetchall()]
                        if "language" not in columns:
                            cursor.execute(migration_sql)
                            cursor.execute(
                                "INSERT INTO schema_version (version) VALUES (?)",
                                (version,),
                            )
                            logger.info(f"마이그레이션 {version} 적용 완료")
                        else:
                            logger.info(
                                f"마이그레이션 {version} 건너뜀 (이미 language 컬럼 존재)"
                            )
                            cursor.execute(
                                "INSERT INTO schema_version (version) VALUES (?)",
                                (version,),
                            )

                    elif version == 2:
                        # keywords 컬럼이 이미 있는지 확인
                        cursor.execute("PRAGMA table_info(articles)")
                        columns = [column[1] for column in cursor.fetchall()]
                        if "keywords" not in columns:
                            cursor.execute(migration_sql)
                            cursor.execute(
                                "INSERT INTO schema_version (version) VALUES (?)",
                                (version,),
                            )
                            logger.info(f"마이그레이션 {version} 적용 완료")
                        else:
                            logger.info(
                                f"마이그레이션 {version} 건너뜀 (이미 keywords 컬럼 존재)"
                            )
                            cursor.execute(
                                "INSERT INTO schema_version (version) VALUES (?)",
                                (version,),
                            )

                    elif version == 3:
                        # search_cache 테이블이 이미 있는지 확인
                        cursor.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='search_cache'"
                        )
                        table_exists = cursor.fetchone()
                        if not table_exists:
                            cursor.execute(migration_sql)
                            cursor.execute(
                                "INSERT INTO schema_version (version) VALUES (?)",
                                (version,),
                            )
                            logger.info(f"마이그레이션 {version} 적용 완료")
                        else:
                            logger.info(
                                f"마이그레이션 {version} 건너뜀 (이미 search_cache 테이블 존재)"
                            )
                            cursor.execute(
                                "INSERT INTO schema_version (version) VALUES (?)",
                                (version,),
                            )

                except Exception as e:
                    logger.warning(f"마이그레이션 {version} 적용 실패: {str(e)}")


class CacheManager:
    """캐시 관리 클래스"""

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager

    def get_cached_results(self, query: str) -> Optional[List[SearchResult]]:
        """캐시된 검색 결과 가져오기"""
        query_hash = hashlib.md5(query.encode("utf-8")).hexdigest()

        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT results FROM search_cache
                    WHERE query_hash = ? AND expires_at > datetime('now')
                """,
                    (query_hash,),
                )

                result = cursor.fetchone()
                if result:
                    try:
                        # JSON 파싱 전에 유효성 검사
                        result_text = result[0]
                        if not result_text or not result_text.strip():
                            logger.warning("캐시된 결과가 비어있음")
                            return None

                        # HTML 태그가 포함된 경우 제거 - 더 안정적인 검사로 개선
                        if (
                            result_text.startswith("<")
                            or result_text.startswith("[")
                            or "<" in result_text
                        ):
                            logger.warning("캐시된 결과에 HTML/JSON 혼합, 캐시 무시")
                            return None

                        data = json.loads(result_text)
                    except json.JSONDecodeError as e:
                        logger.error(f"캐시된 결과 JSON 파싱 오류: {str(e)}")
                        return None
                    except Exception as e:
                        logger.error(f"캐시된 결과 처리 오류: {str(e)}")
                        return None

                    # 캐시된 결과도 제외 목록으로 필터링
                    excluded_sites = self._get_excluded_sites_from_db()
                    excluded_pages = self._get_excluded_pages_from_db()

                    filtered_data = []
                    for item in data:
                        result_obj = SearchResult(**item)
                        if self._should_include_result_cached(
                            result_obj.title,
                            result_obj.summary,
                            result_obj.url,
                            excluded_sites,
                            excluded_pages,
                        ):
                            filtered_data.append(result_obj)
                        else:
                            logger.debug(f"캐시된 결과 필터링 제외: {result_obj.url}")

                    if len(filtered_data) != len(data):
                        logger.info(
                            f"캐시 필터링 후 {len(data) - len(filtered_data)}개 결과 제외됨"
                        )

                    return filtered_data

        except Exception as e:
            logger.error(f"캐시 조회 오류: {str(e)}")

        return None

    def _get_excluded_sites_from_db(self) -> List[str]:
        """데이터베이스에서 제외된 사이트 목록 가져오기"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT domain FROM excluded_sites WHERE active = 1")
                sites = [row[0] for row in cursor.fetchall()]
                logger.debug(f"CacheManager에서 로드된 제외된 사이트: {sites}")
                return sites
        except Exception as e:
            logger.error(f"제외된 사이트 목록 로드 오류: {str(e)}")
            return []

    def _get_excluded_pages_from_db(self) -> List[str]:
        """데이터베이스에서 제외된 페이지 목록 가져오기"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT url FROM excluded_pages WHERE active = 1")
                pages = [row[0] for row in cursor.fetchall()]
                logger.debug(f"CacheManager에서 로드된 제외된 페이지: {pages}")
                return pages
        except Exception as e:
            logger.error(f"제외된 페이지 목록 로드 오류: {str(e)}")
            return []

    def _should_include_result_cached(
        self,
        title: str,
        summary: str,
        url: str,
        excluded_sites: List[str],
        excluded_pages: List[str],
    ) -> bool:
        """캐시된 결과 포함 여부 결정"""
        # 제외된 페이지 필터링
        if any(excluded_page == url for excluded_page in excluded_pages):
            logger.debug(f"캐시된 결과 - 제외된 페이지와 일치: {url}")
            return False

        # 도메인 필터링
        try:
            target_domain = urllib.parse.urlparse(url).netloc.lower()
        except:
            target_domain = ""

        logger.debug(f"캐시된 결과 - 추출된 도메인: {target_domain}")

        excluded_sites_lower = [site.lower() for site in excluded_sites]

        for exclude_site in excluded_sites_lower:
            # 제외 설정된 값에서 도메인만 추출 (프로토콜이 포함된 경우 대비)
            if "://" in exclude_site:
                try:
                    exclude_domain = urllib.parse.urlparse(exclude_site).netloc
                except:
                    exclude_domain = exclude_site
            else:
                # URL이 아닌 경우(예: 경로가 있는 경우) 앞부분만 가져오기 시도
                exclude_domain = exclude_site.split("/")[0]

            exclude_domain = exclude_domain.strip()

            if not exclude_domain:
                continue

            # 도메인 비교
            # 1. 정확히 일치
            # 2. 서브도메인 포함 (예: zhihu.com 입력 시 www.zhihu.com 제외)
            if (
                target_domain == exclude_domain
                or target_domain.endswith("." + exclude_domain)
                or exclude_domain in target_domain
            ):
                logger.debug(
                    f"캐시된 결과 - 제외된 도메인과 일치: {exclude_site} (추출: {exclude_domain}) match {target_domain}"
                )
                return False

        # 모든 조건을 통과하면 포함
        return True

    def cache_results(
        self, query: str, results: List[SearchResult], ttl: int = CACHE_TTL
    ):
        """검색 결과 캐시 저장"""
        query_hash = hashlib.md5(query.encode("utf-8")).hexdigest()
        expires_at = datetime.datetime.now() + datetime.timedelta(seconds=ttl)

        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO search_cache (query_hash, query, results, expires_at)
                    VALUES (?, ?, ?, ?)
                """,
                    (
                        query_hash,
                        query,
                        json.dumps([asdict(result) for result in results]),
                        expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                conn.commit()

        except Exception as e:
            logger.error(f"캐시 저장 오류: {str(e)}")

    def cleanup_expired_cache(self):
        """만료된 캐시 정리"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM search_cache WHERE expires_at <= datetime('now')"
                )
                deleted_count = cursor.rowcount
                conn.commit()
                logger.info(f"만료된 캐시 {deleted_count}개 정리 완료")

        except Exception as e:
            logger.error(f"캐시 정리 오류: {str(e)}")


class SearchEngine:
    """검색 엔진 클래스"""

    def __init__(self, db_manager: DatabaseManager, cache_manager: CacheManager):
        self.db_manager = db_manager
        self.cache_manager = cache_manager
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
        )

    def search_naver(self, query: str, display: int = 10) -> List[SearchResult]:
        """네이버 검색 API를 통해 뉴스 검색 (단순화된 안정성 버전)"""
        try:
            logger.info(f"네이버 검색 시작 (한국어): {query}")

            # API 키 유효성 검증
            if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
                logger.error("네이버 API 키가 설정되지 않았습니다.")
                return []

            # 캐시 확인
            logger.debug(f"캐시 조회 시도: {query}")
            cache_key = f"naver_{query}_{display}"
            cached_results = self.cache_manager.get_cached_results(cache_key)
            if cached_results:
                logger.info(f"네이버 검색 결과 캐시에서 로드: {len(cached_results)}개")
                logger.debug(
                    f"캐시된 결과 URLs: {[result.url for result in cached_results]}"
                )
                # 캐시된 결과도 제외 목록으로 다시 필터링
                excluded_sites = self._get_excluded_sites()
                excluded_pages = self._get_excluded_pages()
                logger.debug(
                    f"캐시 필터링 - 제외된 사이트: {excluded_sites}, 제외된 페이지: {excluded_pages}"
                )

                filtered_cached = []
                for result in cached_results:
                    if self._should_include_result(
                        result.title,
                        result.summary,
                        result.url,
                        excluded_sites,
                        excluded_pages,
                        [],
                    ):
                        filtered_cached.append(result)
                    else:
                        logger.debug(f"캐시된 결과 필터링 제외: {result.url}")

                if len(filtered_cached) != len(cached_results):
                    logger.info(
                        f"캐시 필터링 후 {len(cached_results) - len(filtered_cached)}개 결과 제외됨"
                    )
                    # 필터링된 캐시 결과로 업데이트
                    self.cache_manager.cache_results(cache_key, filtered_cached)
                    return filtered_cached

                return cached_results

            url = "https://openapi.naver.com/v1/search/news.json"
            headers = {
                "X-Naver-Client-Id": NAVER_CLIENT_ID,
                "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
            }
            params = {
                "query": query,
                "display": min(display, 100),  # 최대 100개로 제한
                "sort": "date",
                "start": 1,  # 항상 1부터 시작
            }

            # 단순화된 요청 로직 (web_blog_generator.py 방식)
            try:
                logger.debug(f"네이버 API 요청 전송: {url}")
                response = requests.get(url, headers=headers, params=params, timeout=10)
                logger.debug(f"네이버 API 응답 수신: {response.status_code}")

                if response.status_code == 200:
                    data = response.json()

                    if "error" in data:
                        logger.error(f"네이버 API 에러 응답: {data['error']}")
                        return []

                    items = data.get("items", [])
                    if not items:
                        logger.warning(f"네이버 검색 결과가 없습니다: {query}")
                        return []

                    results = []
                    for item in items:
                        try:
                            # HTML 태그 제거
                            title = self._clean_html_tags(item.get("title", ""))
                            summary = self._clean_html_tags(item.get("description", ""))

                            # 필수 필드 검증
                            if not title or not item.get("link"):
                                logger.debug("필수 필드 누락으로 아이템 건너뜀")
                                continue

                            # 최근 7일 이내 기사만 필터링
                            publish_date = self._format_naver_date(
                                item.get("pubDate", "")
                            )
                            if not self._is_within_last_7_days(publish_date):
                                logger.debug(
                                    f"네이버 날짜 필터링: {publish_date} (7일 초과) - 제외"
                                )
                                continue

                            result = SearchResult(
                                title=title,
                                summary=summary,
                                url=item.get("link", ""),
                                source="Naver",
                                publish_date=publish_date,
                                keywords=[query],
                                language="ko",
                            )
                            result.relevance_score = self._calculate_relevance_score(
                                result
                            )

                            # 제외된 페이지 필터링 추가
                            excluded_sites = self._get_excluded_sites()
                            excluded_pages = self._get_excluded_pages()
                            if not self._should_include_result(
                                result.title,
                                result.summary,
                                result.url,
                                excluded_sites,
                                excluded_pages,
                                [],
                            ):
                                logger.debug(
                                    f"네이버 검색 결과 필터링 제외 (제외된 페이지): {result.url}"
                                )
                                continue

                            results.append(result)

                        except Exception as item_error:
                            logger.warning(f"아이템 처리 중 오류: {str(item_error)}")
                            continue

                    # 결과 캐시 저장
                    if results:
                        self.cache_manager.cache_results(cache_key, results)
                        logger.info(
                            f"네이버 검색 성공: {len(results)}개 (최근 7일 이내)"
                        )
                        return results
                    else:
                        logger.warning("처리 가능한 결과가 없습니다.")
                        return []

                else:
                    logger.error(f"네이버 API 오류: {response.status_code}")
                    return []

            except requests.exceptions.RequestException as e:
                logger.error(f"네이버 API 요청 실패: {str(e)}")
                return []

        except Exception as e:
            logger.error(f"네이버 검색 치명적 오류: {str(e)}")
            return []

    def search_duckduckgo(
        self, query: str, num_results: int = 10
    ) -> List[SearchResult]:
        """DuckDuckGo 검색 (실패 시 Tavily 대체)"""
        try:
            logger.info(f"DuckDuckGo 검색 시작 (영어): {query}")

            # 캐시 확인
            cache_key = f"duckduckgo_{query}_{num_results}"
            cached_results = self.cache_manager.get_cached_results(cache_key)
            if cached_results:
                logger.info(f"DuckDuckGo 캐시에서 로드: {len(cached_results)}개")
                return cached_results

            # 영어 검색어로 변환
            english_query = self._convert_to_english_query(query)
            clean_query = re.sub(r"[^\w\s-]", "", english_query).strip()
            if not clean_query:
                logger.warning("영어 검색어가 유효하지 않음")
                return []

            logger.info(f"변환된 영어 검색어: {clean_query}")

            search_results = []

            # 방법 1: 최신 ddgs 라이브러리
            try:
                from ddgs import DDGS

                logger.debug("최신 ddgs 라이브러리 사용 시도")

                ddgs = DDGS()

                try:
                    results = list(
                        ddgs.text(
                            clean_query,
                            max_results=num_results * 2,
                            timeout=20,
                        )
                    )
                    search_results.extend(results)
                    logger.info(f"ddgs 검색 성공: {len(results)}개 결과")

                except Exception as ddgs_error:
                    logger.warning(f"ddgs 검색 실패: {str(ddgs_error)}")

            except ImportError:
                logger.debug("ddgs 라이브러리 없음, 다른 방법 시도")
            except Exception as ddgs_init_error:
                logger.warning(f"ddgs 초기화 실패: {str(ddgs_init_error)}")

            # 방법 2: 이전 버전 duckduckgo_search 라이브러리
            if not search_results:
                try:
                    from duckduckgo_search import DDGS

                    logger.debug("이전 버전 duckduckgo_search 라이브러리 사용 시도")

                    ddgs = DDGS()
                    results = list(ddgs.text(clean_query, max_results=num_results * 2))
                    search_results.extend(results)
                    logger.info(f"duckduckgo_search 검색 성공: {len(results)}개 결과")

                except ImportError:
                    logger.debug("duckduckgo_search 라이브러리 없음")
                except Exception as legacy_error:
                    logger.warning(f"이전 버전 라이브러리 실패: {str(legacy_error)}")

            # DuckDuckGo 실패 시 Tavily 대체
            if not search_results:
                logger.warning("DuckDuckGo 검색 실패, Tavily 대제 시도")
                return self.search_tavily(query, num_results)

            logger.debug(f"DuckDuckGo 총 검색 결과: {len(search_results)}개")

            results = []

            # 제외 목록 가져오기
            excluded_sites = self._get_excluded_sites()
            excluded_pages = self._get_excluded_pages()

            # 디버깅 로그 추가
            logger.info(
                f"DuckDuckGo 검색 필터링 시작 - 제외된 사이트: {len(excluded_sites)}개, 제외된 페이지: {len(excluded_pages)}개"
            )

            # 제외 키워드 목록
            exclude_keywords = [
                "github",
                "documentation",
                "api reference",
                "official website",
                "home page",
                "wikipedia",
                "wiki",
                "tutorial",
                "getting started",
                "installation",
                "download",
                "pricing",
                "about",
                "company",
                "careers",
                "contact",
                "privacy",
                "terms",
                "policy",
            ]

            # 결과를 날짜 순으로 정렬하기 위한 임시 리스트
            results_with_dates = []

            for result in search_results:
                if not isinstance(result, dict):
                    continue

                title = result.get("title", "")
                url = result.get("href", "")
                summary = result.get("body", "")

                if not title or not url:
                    continue

                # 필터링 적용
                if not self._should_include_result(
                    title,
                    summary,
                    url,
                    excluded_sites,
                    excluded_pages,
                    exclude_keywords,
                ):
                    continue

                # 날짜 처리
                extracted_date = self._extract_date_from_content_enhanced(
                    title, summary, url
                )

                if not extracted_date:
                    logger.debug(f"날짜를 추출할 수 없어 제외: {title[:50]}...")
                    continue

                if not self._is_within_last_15_days_strict(
                    extracted_date, title, summary
                ):
                    logger.debug(
                        f"날짜 필터링: {extracted_date} (15일 초과) - 제외: {title[:50]}..."
                    )
                    continue

                publish_date = extracted_date
                results_with_dates.append((result, publish_date, title, summary, url))

            # 날짜순으로 정렬
            results_with_dates.sort(key=lambda x: x[1], reverse=True)

            # 정렬된 결과에서 최종 결과 생성
            for result_data, publish_date, title, summary, url in results_with_dates:
                try:
                    original_lang = self._detect_language(title + " " + summary)

                    if original_lang == "en":
                        translated_title = self._translate_with_google(title)
                        translated_summary = self._translate_with_google(summary)
                    elif original_lang == "ko":
                        translated_title = title
                        translated_summary = summary
                    else:
                        translated_title = self._translate_with_google(title)
                        translated_summary = self._translate_with_google(summary)

                    if original_lang == "en":
                        if (
                            not translated_title
                            or translated_title == title
                            or len(translated_title.strip()) == 0
                        ):
                            translated_title = self._fallback_translate(title)

                        if (
                            not translated_summary
                            or translated_summary == summary
                            or len(translated_summary.strip()) == 0
                        ):
                            translated_summary = self._fallback_translate(summary)

                except Exception as translate_error:
                    logger.error(f"DuckDuckGo 결과 번역 오류: {str(translate_error)}")
                    translated_title = self._fallback_translate(title)
                    translated_summary = self._fallback_translate(summary)

                result_obj = SearchResult(
                    title=translated_title,
                    summary=translated_summary,
                    url=url,
                    source="DuckDuckGo",
                    publish_date=publish_date,
                    keywords=[query],
                    language="ko",
                )
                result_obj.relevance_score = self._calculate_relevance_score(result_obj)

                if not self._should_include_result(
                    result_obj.title,
                    result_obj.summary,
                    result_obj.url,
                    excluded_sites,
                    excluded_pages,
                    [],
                ):
                    continue

                results.append(result_obj)

                if len(results) >= num_results:
                    break

            # 결과 캐시 저장
            self.cache_manager.cache_results(cache_key, results)

            logger.info(f"DuckDuckGo 검색 결과: {len(results)}개 (필터링됨)")
            return results

        except Exception as e:
            logger.error(f"DuckDuckGo 검색 오류: {str(e)}")
            # 예외 발생 시 Tavily 대체 시도
            logger.info("DuckDuckGo 예외로 인해 Tavily 대체 시도")
            return self.search_tavily(query, num_results)

    def search_tavily(self, query: str, num_results: int = 10) -> List[SearchResult]:
        """Tavily Search API 검색"""
        try:
            logger.info(f"Tavily 검색 시작: {query}")

            # API 키 확인
            if not TAVILY_API_KEY:
                logger.error("TAVILY_API_KEY가 설정되지 않음")
                return []

            # 캐시 확인
            cache_key = f"tavily_{query}_{num_results}"
            cached_results = self.cache_manager.get_cached_results(cache_key)
            if cached_results:
                logger.info(f"Tavily 캐시에서 로드: {len(cached_results)}개")
                return cached_results

            # 영어 검색어로 변환
            english_query = self._convert_to_english_query(query)
            clean_query = re.sub(r"[^\w\s-]", "", english_query).strip()
            if not clean_query:
                logger.warning("영어 검색어가 유효하지 않음")
                return []

            logger.info(f"변환된 영어 검색어: {clean_query}")

            # Tavily API 호출
            from tavily import TavilyClient

            client = TavilyClient(api_key=TAVILY_API_KEY)

            # Tavily Search API 실행 (days 파라미터 제거 - API에서 지원하지 않음)
            response = client.search(
                query=clean_query,
                max_results=num_results * 2,  # 필터링을 위해 더 많은 결과 요청
                search_depth="advanced",
                include_answer=False,
                include_raw_content=False,
                include_images=False,
            )

            # API 응답 디버깅
            logger.info(f"Tavily API 응답: {list(response.keys()) if isinstance(response, dict) else type(response)}")
            if isinstance(response, dict) and "results" in response:
                logger.info(f"Tavily results 개수: {len(response.get('results', []))}")
                if response.get("results"):
                    logger.debug(f"첫 번째 결과 키: {list(response['results'][0].keys())}")

            # 결과 파싱
            search_results = []
            for result in response.get("results", []):
                search_results.append({
                    "title": result.get("title", ""),
                    "href": result.get("url", ""),
                    "body": result.get("content", ""),
                    "score": result.get("score", 0.0),
                    "published_date": result.get("published_date", ""),  # Tavily 제공 날짜
                })

            if not search_results:
                logger.warning("Tavily 검색 결과 없음")
                return []

            logger.info(f"Tavily 총 검색 결과: {len(search_results)}개")

            results = []

            # 제외 목록 가져오기
            excluded_sites = self._get_excluded_sites()
            excluded_pages = self._get_excluded_pages()

            # 디버깅 로그 추가
            logger.info(
                f"Tavily 검색 필터링 시작 - 제외된 사이트: {len(excluded_sites)}개, 제외된 페이지: {len(excluded_pages)}개"
            )
            logger.debug(f"제외된 사이트 목록: {excluded_sites}")
            logger.debug(f"제외된 페이지 목록: {excluded_pages}")

            # 제외 키워드 목록
            exclude_keywords = [
                "github",
                "documentation",
                "api reference",
                "official website",
                "home page",
                "wikipedia",
                "wiki",
                "tutorial",
                "getting started",
                "installation",
                "download",
                "pricing",
                "about",
                "company",
                "careers",
                "contact",
                "privacy",
                "terms",
                "policy",
            ]

            # 결과를 날짜 순으로 정렬하기 위한 임시 리스트
            results_with_dates = []

            for result in search_results:
                title = result.get("title", "")
                url = result.get("href", "")
                summary = result.get("body", "")
                tavily_date = result.get("published_date", "")

                if not title or not url:
                    continue

                # 필터링 적용
                if not self._should_include_result(
                    title,
                    summary,
                    url,
                    excluded_sites,
                    excluded_pages,
                    exclude_keywords,
                ):
                    continue

                # 날짜 처리 - Tavily 제공 날짜 우선 사용
                extracted_date = None

                # 1. Tavily가 제공하는 날짜 우선 사용
                if tavily_date:
                    try:
                        # Tavily 날짜 포맷 처리 (ISO 8601 등)
                        from datetime import datetime
                        # 다양한 날짜 포맷 시도
                        for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"]:
                            try:
                                extracted_date = datetime.strptime(tavily_date.split("T")[0][:10], "%Y-%m-%d").strftime("%Y-%m-%d")
                                break
                            except:
                                continue
                        if not extracted_date:
                            extracted_date = tavily_date.split("T")[0][:10]  # YYYY-MM-DD 형식 추출 시도
                        logger.debug(f"Tavily 제공 날짜 사용: {extracted_date}")
                    except Exception as e:
                        logger.debug(f"Tavily 날짜 파싱 실패: {tavily_date}, 오류: {e}")

                # 2. Tavily 날짜가 없는 경우 내용에서 날짜 추출
                if not extracted_date:
                    extracted_date = self._extract_date_from_content_enhanced(
                        title, summary, url
                    )

                # 날짜가 없는 경우 엄격하게 제외 (오래된 기사 방지)
                if not extracted_date:
                    logger.debug(
                        f"날짜를 추출할 수 없어 엄격하게 제외: {title[:50]}..."
                    )
                    continue

                # 최근 7일 이내인지 확인 (Tavily API days=7과 맞춤)
                if not self._is_within_last_7_days_strict(
                    extracted_date, title, summary
                ):
                    logger.debug(
                        f"7일 초과 날짜 필터링: {extracted_date} - 제외: {title[:50]}..."
                    )
                    continue

                publish_date = extracted_date

                # 날짜 정보와 함께 결과 저장
                results_with_dates.append((result, publish_date, title, summary, url))

            # 날짜순으로 정렬 (최신 날짜가 먼저 오도록)
            results_with_dates.sort(key=lambda x: x[1], reverse=True)

            # 정렬된 결과에서 최종 결과 생성
            for result_data, publish_date, title, summary, url in results_with_dates:
                # 강제 번역 적용 (영어 결과를 한국어로)
                try:
                    # 원본 언어 감지
                    original_lang = self._detect_language(title + " " + summary)
                    logger.debug(f"감지된 원본 언어: {original_lang}")

                    # 영어인 경우 번역 시도
                    if original_lang == "en":
                        # 영어인 경우 구글 무료 번역으로 번역
                        translated_title = self._translate_with_google(title)
                        translated_summary = self._translate_with_google(summary)
                        logger.debug(f"영어->한국어 번역 시도: {title[:30]}...")
                    elif original_lang == "ko":
                        # 이미 한국어인 경우 그대로 사용
                        translated_title = title
                        translated_summary = summary
                        logger.debug(f"한국어 텍스트로 번역 건너뜀: {title[:30]}...")
                    else:
                        # 기타 언어도 번역 시도
                        translated_title = self._translate_with_google(title)
                        translated_summary = self._translate_with_google(summary)
                        logger.debug(
                            f"기타 언어({original_lang})->한국어 번역 시도: {title[:30]}..."
                        )

                    # 번역 결과 검증 및 강제 적용
                    if original_lang == "en":
                        # 영어인 경우 번역이 실패하면 대체 번역 방법 시도
                        if (
                            not translated_title
                            or translated_title == title
                            or len(translated_title.strip()) == 0
                        ):
                            logger.warning(
                                f"영어 제목 번역 실패, 대체 번역 시도: {title[:30]}..."
                            )
                            translated_title = self._fallback_translate(title)

                        if (
                            not translated_summary
                            or translated_summary == summary
                            or len(translated_summary.strip()) == 0
                        ):
                            logger.warning(
                                f"영어 요약 번역 실패, 대체 번역 시도: {summary[:30]}..."
                            )
                            translated_summary = self._fallback_translate(summary)

                except Exception as translate_error:
                    logger.error(
                        f"Tavily 결과 번역 중 심각한 오류: {str(translate_error)}"
                    )
                    # 대체 번역 방법 사용
                    translated_title = self._fallback_translate(title)
                    translated_summary = self._fallback_translate(summary)

                result_obj = SearchResult(
                    title=translated_title,
                    summary=translated_summary,
                    url=url,
                    source="Tavily",
                    publish_date=publish_date,
                    keywords=[query],
                    language="ko",
                )
                result_obj.relevance_score = self._calculate_relevance_score(result_obj)

                # 제외된 페이지 필터링 추가
                excluded_sites = self._get_excluded_sites()
                excluded_pages = self._get_excluded_pages()
                if not self._should_include_result(
                    result_obj.title,
                    result_obj.summary,
                    result_obj.url,
                    excluded_sites,
                    excluded_pages,
                    [],
                ):
                    logger.debug(
                        f"Tavily 검색 결과 필터링 제외 (제외된 페이지): {result_obj.url}"
                    )
                    continue

                results.append(result_obj)

                if len(results) >= num_results:
                    break

            # 결과 캐시 저장
            self.cache_manager.cache_results(cache_key, results)

            logger.info(f"Tavily 검색 결과: {len(results)}개 (필터링됨)")
            logger.debug(f"최종 결과 URLs: {[result.url for result in results]}")
            return results

        except Exception as e:
            logger.error(f"Tavily 검색 오류: {str(e)}")
            return []

    def _get_excluded_sites(self) -> List[str]:
        """제외된 사이트 목록 가져오기"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT domain FROM excluded_sites WHERE active = 1")
                return [row[0] for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"제외된 사이트 목록 로드 오류: {str(e)}")
            return []

    def _get_excluded_pages(self) -> List[str]:
        """제외된 페이지 목록 가져오기"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT url FROM excluded_pages WHERE active = 1")
                pages = [row[0] for row in cursor.fetchall()]
                logger.debug(f"데이터베이스에서 로드된 제외된 페이지: {pages}")
                return pages
        except Exception as e:
            logger.error(f"제외된 페이지 목록 로드 오류: {str(e)}")
            return []

    def _should_include_result(
        self,
        title: str,
        summary: str,
        url: str,
        excluded_sites: List[str],
        excluded_pages: List[str],
        exclude_keywords: List[str],
    ) -> bool:
        """결과 포함 여부 결정 (최적화된 필터링)"""
        # 디버깅 로그 추가
        logger.debug(f"필터링 검사 시작: URL={url}")
        logger.debug(f"제외된 사이트 목록: {excluded_sites}")
        logger.debug(f"제외된 페이지 목록: {excluded_pages}")

        # 제외된 페이지 필터링
        if any(excluded_page == url for excluded_page in excluded_pages):
            logger.debug(f"제외된 페이지와 일치: {url}")
            return False

        # 도메인 필터링
        try:
            target_domain = urllib.parse.urlparse(url).netloc.lower()
        except:
            target_domain = ""

        logger.debug(f"추출된 도메인: {target_domain}")

        excluded_sites_lower = [site.lower() for site in excluded_sites]

        for exclude_site in excluded_sites_lower:
            # 제외 설정된 값에서 도메인만 추출 (프로토콜이 포함된 경우 대비)
            if "://" in exclude_site:
                try:
                    exclude_domain = urllib.parse.urlparse(exclude_site).netloc
                except:
                    exclude_domain = exclude_site
            else:
                # URL이 아닌 경우(예: 경로가 있는 경우) 앞부분만 가져오기 시도
                exclude_domain = exclude_site.split("/")[0]

            exclude_domain = exclude_domain.strip()

            if not exclude_domain:
                continue

            # 도메인 비교
            # 1. 정확히 일치
            # 2. 서브도메인 포함 (예: zhihu.com 입력 시 www.zhihu.com 제외)
            if (
                target_domain == exclude_domain
                or target_domain.endswith("." + exclude_domain)
                or exclude_domain in target_domain
            ):
                logger.debug(
                    f"제외된 도메인과 일치: {exclude_site} (추출: {exclude_domain}) match {target_domain}"
                )
                return False

        title_lower = title.lower()
        summary_lower = summary.lower()
        combined_text = f"{title_lower} {summary_lower}"

        # 엄격한 제외 키워드 목록 (AI 관련이 아닌 일반 콘텐츠)
        strict_exclude_keywords = [
            "porn",
            "adult",
            "gambling",
            "casino",
            "betting",
            "shopping cart",
            "add to cart",
            "buy now",
            "price",
            "recipe",
            "cooking",
            "food",
            "restaurant",
            "sports score",
            "game score",
            "weather forecast",
            "stock price",
            "investment advice",
            "cryptocurrency price",
        ]

        # 중간 제외 키워드 목록 (AI 관련이지만 저품질 콘텐츠)
        medium_exclude_keywords = [
            "documentation",
            "api reference",
            "official website",
            "home page",
            "wikipedia",
            "wiki",
            "getting started",
            "installation",
            "download",
            "pricing",
            "about",
            "company",
            "careers",
            "contact",
            "privacy",
            "terms",
            "policy",
            "login",
            "register",
            "signup",
            "account",
        ]

        # 엄격한 제외 키워드가 있으면 즉시 제외
        if any(
            exclude_keyword in combined_text
            for exclude_keyword in strict_exclude_keywords
        ):
            return False

        # AI 관련 키워드 필수 포함 (확장된 목록)
        ai_keywords = [
            "ai",
            "artificial intelligence",
            "machine learning",
            "deep learning",
            "llm",
            "chatgpt",
            "claude",
            "gemini",
            "gpt",
            "openai",
            "anthropic",
            "deepseek",
            "qwen",
            "alibaba",
            "nvidia",
            "microsoft",
            "google",
            "meta",
            "xai",
            "grok",
            "huggingface",
            "transformer",
            "neural network",
            "copilot",
            "github",
            "coding",
            "programming",
            "development",
            "api",
            "model",
            "algorithm",
            "automation",
            "robotics",
            "computer vision",
            "python",
            "javascript",
            "framework",
            "library",
            "dataset",
            "training",
            "inference",
            "fine-tuning",
            "prompt engineering",
            "rag",
            "retrieval augmented generation",
            "vector database",
            "embedding",
            "diffusion",
            "generative ai",
            "nlp",
        ]

        # AI 관련 키워드가 있는지 확인
        has_ai_keyword = any(ai_keyword in combined_text for ai_keyword in ai_keywords)

        # AI 관련 키워드가 없으면 제외
        if not has_ai_keyword:
            return False

        # 중간 제외 키워드가 있지만 AI 관련 키워드가 2개 이상이면 포함
        has_medium_exclude = any(
            exclude_keyword in combined_text
            for exclude_keyword in medium_exclude_keywords
        )
        ai_keyword_count = sum(
            1 for ai_keyword in ai_keywords if ai_keyword in combined_text
        )

        if has_medium_exclude and ai_keyword_count < 2:
            return False

        # 최신성 확인 (최근 30일 이내 기사 우선)
        current_date = datetime.datetime.now()
        try:
            # URL이나 제목에서 날짜 추출 시도
            date_in_content = self._extract_date_from_content(title, summary)
            if date_in_content:
                article_date = datetime.datetime.strptime(date_in_content, "%Y-%m-%d")
                days_old = (current_date - article_date).days
                # 30일 이상 된 기사는 AI 관련 키워드가 3개 이상이어야 함
                if days_old > 30 and ai_keyword_count < 3:
                    return False
        except:
            pass

        # 최신 AI 키워드 확인 (추가된 필터링)
        if not self._contains_recent_ai_keywords(title, summary):
            logger.debug(f"최신 AI 키워드가 없어 제외: {title[:50]}...")
            return False

        # 모든 조건을 통과하면 포함
        return True

    def _clean_html_tags(self, text: str) -> str:
        """HTML 태그 제거"""
        if not text:
            return ""

        # HTML 태그 제거
        text = re.sub(r"<[^>]*>", "", text)
        # HTML 엔티티 디코딩
        text = (
            text.replace('&quot;', '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        )
        text = text.replace("<", "<").replace(">", ">").replace("&", "&")
        return text.strip()

    def _format_naver_date(self, date_str: str) -> str:
        """네이버 날짜 형식 변환 (개선된 버전)"""
        if not date_str:
            return datetime.datetime.now().strftime("%Y-%m-%d")

        try:
            # 1. 네이버 API 표준 형식: "Mon, 25 Nov 2025 12:30:00 +0900"
            try:
                date_obj = datetime.datetime.strptime(
                    date_str, "%a, %d %b %Y %H:%M:%S %z"
                )
                return date_obj.strftime("%Y-%m-%d")
            except:
                pass

            # 2. 다른 네이버 형식 시도
            alternative_formats = [
                "%a, %d %b %Y %H:%M:%S %z",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
                "%Y/%m/%d",
                "%d %b %Y",
                "%b %d, %Y",
            ]

            for fmt in alternative_formats:
                try:
                    date_obj = datetime.datetime.strptime(date_str, fmt)
                    return date_obj.strftime("%Y-%m-%d")
                except:
                    continue

            # 3. 콘텐츠에서 날짜 추출 시도
            extracted_date = self._extract_date_from_content(date_str, "")
            if extracted_date:
                return extracted_date

        except Exception as e:
            logger.warning(f"네이버 날짜 변환 실패: {date_str} - {str(e)}")

        # 모든 변환이 실패하면 현재 날짜 반환
        return datetime.datetime.now().strftime("%Y-%m-%d")

    def _extract_date_from_content(self, title: str, summary: str) -> Optional[str]:
        """콘텐츠에서 날짜 추출 (개선된 버전)"""
        text = f"{title} {summary}"

        # 다양한 날짜 패턴 매칭 (대폭 개선된 버전)
        date_patterns = [
            # 기본 형식
            r"\b(\d{4}-\d{2}-\d{2})\b",
            r"\b(\d{4}/\d{2}/\d{2})\b",
            r"\b(\d{2})/(\d{2})/(\d{4})\b",
            r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b",
            # 영어 월명 형식 (확장)
            r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\b",
            r"\b((Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4})\b",
            r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.\s+\d{1,2},\s+\d{4})\b",
            # "Posted/Updated" 접두사 형식 (대폭 확장)
            r"\b(?:Posted|Updated|Published|Released)\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[\.\s]*\d{1,2},\s+\d{4}\b",
            r"\b(?:Posted|Updated|Published|Released)\s+\d{1,2}[\.\s]*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\b",
            # 복합 문장 형식 (사용자 예시 특화)
            r"(?:Posted|Updated|Published|Released)\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[\.\s]*\d{1,2},\s+\d{4}",
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[\.\s]*\d{1,2},\s+\d{4}",
            # 한국어 형식 (대폭 확장)
            r"\b(\d{4})년\s+(\d{1,2})월\s+(\d{1,2})일\b",
            r"\b(\d{4})년(\d{1,2})월(\d{1,2})일\b",
            r"\b(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\b",
            # ISO 8601 형식 (확장)
            r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\b",
            r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3})\b",
            r"\b(\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2})\b",
        ]

        for pattern in date_patterns:
            try:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    date_str = (
                        match.group(1) if match.lastindex >= 1 else match.group(0)
                    )

                    # 날짜 형식 변환 시도 (대폭 개선된 버전)
                    if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
                        return date_str
                    elif re.match(r"\d{4}/\d{2}/\d{2}", date_str):
                        return date_str.replace("/", "-")
                    elif re.match(r"\d{2}/\d{2}/\d{4}", date_str):
                        parts = date_str.split("/")
                        return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
                    elif re.match(r"\d{1,2}[-/]\d{1,2}[-/]\d{4}", date_str):
                        # 다양한 구분자 처리
                        parts = re.split(r"[-/]", date_str)
                        if len(parts) == 3:
                            return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
                    elif re.match(
                        r"\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}",
                        date_str,
                        re.IGNORECASE,
                    ):
                        return datetime.datetime.strptime(
                            date_str, "%d %b %Y"
                        ).strftime("%Y-%m-%d")
                    elif re.match(
                        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}",
                        date_str,
                        re.IGNORECASE,
                    ):
                        return datetime.datetime.strptime(
                            date_str, "%b %d, %Y"
                        ).strftime("%Y-%m-%d")
                    elif re.match(
                        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.\s+\d{1,2},\s+\d{4}",
                        date_str,
                        re.IGNORECASE,
                    ):
                        # 점(.) 포함 월명 형식 처리
                        clean_date = date_str.replace(".", "")
                        return datetime.datetime.strptime(
                            clean_date, "%b %d, %Y"
                        ).strftime("%Y-%m-%d")
                    elif re.match(
                        r"(?:Posted|Updated|Published|Released)\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[\.\s]*\d{1,2},\s+\d{4}",
                        date_str,
                        re.IGNORECASE,
                    ):
                        # Posted/Updated + 점(.) 포함 형식 (우선 처리)
                        date_only = re.sub(
                            r"^(?:Posted|Updated|Published|Released)\s+",
                            "",
                            date_str,
                            flags=re.IGNORECASE,
                        ).replace(".", "")
                        return datetime.datetime.strptime(
                            date_only, "%b %d, %Y"
                        ).strftime("%Y-%m-%d")
                    elif re.match(
                        r"(?:Posted|Updated|Published|Released)\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}",
                        date_str,
                        re.IGNORECASE,
                    ):
                        # Posted/Updated 접두사 제거 후 날짜만 추출
                        date_only = re.sub(
                            r"^(?:Posted|Updated|Published|Released)\s+",
                            "",
                            date_str,
                            flags=re.IGNORECASE,
                        )
                        if "." in date_only:
                            date_only = date_only.replace(".", "")
                        return datetime.datetime.strptime(
                            date_only, "%b %d, %Y"
                        ).strftime("%Y-%m-%d")
                    elif re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", date_str):
                        # ISO 8601 형식 (날짜 부분만)
                        return date_str.split("T")[0]
                    elif re.match(
                        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}", date_str
                    ):
                        # ISO 8601 형식 (소수점 포함)
                        return date_str.split("T")[0]
                    elif re.match(r"\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}", date_str):
                        # ISO 8601 소문자 t 형식
                        return date_str.split("t")[0]
                    elif (
                        "년" in date_str
                        and ("월" in date_str or "월" in date_str)
                        and ("일" in date_str or "일" in date_str)
                    ):
                        # 한국어 날짜 형식 (대폭 개선)
                        clean_date = date_str.replace(" ", "")
                        korean_match = re.match(
                            r"(\d{4})년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", clean_date
                        )
                        if korean_match:
                            year, month, day = korean_match.groups()
                            return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
                        # 다른 한국어 형식 시도
                        korean_match2 = re.match(
                            r"(\d{4})년(\d{1,2})월(\d{1,2})일", clean_date
                        )
                        if korean_match2:
                            year, month, day = korean_match2.groups()
                            return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

            except Exception as e:
                logger.warning(f"날짜 변환 실패: {date_str} - {str(e)}")
                continue

        return None

    def _is_within_last_7_days(self, date_str: str) -> bool:
        """최근 7일 이내인지 확인 (개선된 버전)"""
        if not date_str:
            return False

        try:
            # 이미 YYYY-MM-DD 형식이 아닌 경우 변환 시도
            if not re.match(r"\d{4}-\d{2}-\d{2}", date_str):
                extracted_date = self._extract_date_from_content(date_str, "")
                if extracted_date:
                    date_str = extracted_date
                else:
                    logger.warning(f"날짜 형식을 인식할 수 없음: {date_str}")
                    return False

            # 날짜 파싱
            date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d")

            # 현재 시간과 비교 (시간대 고려)
            now = datetime.datetime.now()
            seven_days_ago = now - datetime.timedelta(days=7)

            # 7일 경계 포함 (7일 전이어야 함)
            is_recent = date_obj >= seven_days_ago

            # 디버깅 로그
            if not is_recent:
                days_diff = (now - date_obj).days
                logger.debug(
                    f"날짜 필터링: {date_str} ({days_diff}일 전) - 7일 초과로 제외"
                )
            else:
                days_diff = (now - date_obj).days
                logger.debug(f"날짜 포함: {date_str} ({days_diff}일 전) - 7일 이내")

            return is_recent

        except Exception as e:
            logger.error(f"날짜 필터링 오류: {date_str} - {str(e)}")
            return False

    def _is_within_last_7_days_strict(
        self, date_str: str, title: str = "", summary: str = ""
    ) -> bool:
        """최근 7일 이내인지 확인 (더 엄격한 버전)"""
        if not date_str:
            return False

        try:
            # 이미 YYYY-MM-DD 형식이 아닌 경우 변환 시도
            if not re.match(r"\d{4}-\d{2}-\d{2}", date_str):
                extracted_date = self._extract_date_from_content_enhanced(
                    title, summary, ""
                )
                if extracted_date:
                    date_str = extracted_date
                else:
                    logger.warning(f"날짜 형식을 인식할 수 없음: {date_str}")
                    return False

            # 날짜 파싱
            date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d")

            # 현재 시간과 비교 (시간대 고려)
            now = datetime.datetime.now()
            seven_days_ago = now - datetime.timedelta(days=7)

            # 7일 경계 포함 (7일 전이어야 함)
            is_recent = date_obj >= seven_days_ago

            # 디버깅 로그
            if not is_recent:
                days_diff = (now - date_obj).days
                logger.debug(
                    f"엄격한 날짜 필터링: {date_str} ({days_diff}일 전) - 7일 초과로 제외"
                )
            else:
                days_diff = (now - date_obj).days
                logger.debug(
                    f"엄격한 날짜 포함: {date_str} ({days_diff}일 전) - 7일 이내"
                )

            return is_recent

        except Exception as e:
            logger.error(f"엄격한 날짜 필터링 오류: {date_str} - {str(e)}")
            return False

    def _is_within_last_15_days_strict(
        self, date_str: str, title: str = "", summary: str = ""
    ) -> bool:
        """최근 15일 이내인지 확인 (완화된 버전)"""
        if not date_str:
            return False

        try:
            # 이미 YYYY-MM-DD 형식이 아닌 경우 변환 시도
            if not re.match(r"\d{4}-\d{2}-\d{2}", date_str):
                extracted_date = self._extract_date_from_content_enhanced(
                    title, summary, ""
                )
                if extracted_date:
                    date_str = extracted_date
                else:
                    logger.warning(f"날짜 형식을 인식할 수 없음: {date_str}")
                    return False

            # 날짜 파싱
            date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d")

            # 현재 시간과 비교 (시간대 고려)
            now = datetime.datetime.now()
            fifteen_days_ago = now - datetime.timedelta(days=15)

            # 15일 경계 포함 (15일 전이어야 함)
            is_recent = date_obj >= fifteen_days_ago

            # 디버깅 로그
            if not is_recent:
                days_diff = (now - date_obj).days
                logger.debug(
                    f"완화된 날짜 필터링: {date_str} ({days_diff}일 전) - 15일 초과로 제외"
                )
            else:
                days_diff = (now - date_obj).days
                logger.debug(
                    f"완화된 날짜 포함: {date_str} ({days_diff}일 전) - 15일 이내"
                )

            return is_recent

        except Exception as e:
            logger.error(f"완화된 날짜 필터링 오류: {date_str} - {str(e)}")
            return False

    def _is_within_last_3_days_strict(
        self, date_str: str, title: str = "", summary: str = ""
    ) -> bool:
        """최근 3일 이내인지 확인 (매우 엄격한 버전)"""
        if not date_str:
            return False

        try:
            # 이미 YYYY-MM-DD 형식이 아닌 경우 변환 시도
            if not re.match(r"\d{4}-\d{2}-\d{2}", date_str):
                extracted_date = self._extract_date_from_content_enhanced(
                    title, summary, ""
                )
                if extracted_date:
                    date_str = extracted_date
                else:
                    logger.warning(f"날짜 형식을 인식할 수 없음: {date_str}")
                    return False

            # 날짜 파싱
            date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d")

            # 현재 시간과 비교 (시간대 고려)
            now = datetime.datetime.now()
            three_days_ago = now - datetime.timedelta(days=3)

            # 3일 경계 포함 (3일 전이어야 함)
            is_recent = date_obj >= three_days_ago

            # 디버깅 로그
            if not is_recent:
                days_diff = (now - date_obj).days
                logger.debug(
                    f"초엄격 날짜 필터링: {date_str} ({days_diff}일 전) - 3일 초과로 제외"
                )
            else:
                days_diff = (now - date_obj).days
                logger.debug(
                    f"초엄격 날짜 포함: {date_str} ({days_diff}일 전) - 3일 이내"
                )

            return is_recent

        except Exception as e:
            logger.error(f"초엄격 날짜 필터링 오류: {date_str} - {str(e)}")
            return False

    def _is_within_last_7_days_strict(
        self, date_str: str, title: str = "", summary: str = ""
    ) -> bool:
        """최근 7일 이내인지 확인 (Tavily용)"""
        if not date_str:
            return False

        try:
            # 이미 YYYY-MM-DD 형식이 아닌 경우 변환 시도
            if not re.match(r"\d{4}-\d{2}-\d{2}", date_str):
                extracted_date = self._extract_date_from_content_enhanced(
                    title, summary, ""
                )
                if extracted_date:
                    date_str = extracted_date
                else:
                    logger.warning(f"날짜 형식을 인식할 수 없음: {date_str}")
                    return False

            # 날짜 파싱
            date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d")

            # 현재 시간과 비교 (시간대 고려)
            now = datetime.datetime.now()
            seven_days_ago = now - datetime.timedelta(days=7)

            # 7일 경계 포함 (7일 전이어야 함)
            is_recent = date_obj >= seven_days_ago

            # 디버깅 로그
            if not is_recent:
                days_diff = (now - date_obj).days
                logger.debug(
                    f"7일 날짜 필터링: {date_str} ({days_diff}일 전) - 7일 초과로 제외"
                )

            return is_recent

        except Exception as e:
            logger.error(f"7일 날짜 필터링 오류: {date_str} - {str(e)}")
            return False

    def _extract_date_from_content_enhanced(
        self, title: str, summary: str, url: str = ""
    ) -> Optional[str]:
        """콘텐츠에서 날짜 추출 (강화된 버전)"""
        text = f"{title} {summary}"

        # 1. 페이지 내용에서 날짜 추출 (우선 순위 최상위)
        content_date = self._extract_date_from_page_content(title, summary)
        if content_date:
            logger.debug(f"페이지 내용에서 날짜 추출 성공: {content_date}")
            return content_date

        # 2. 기존 날짜 추출 시도
        existing_date = self._extract_date_from_content(title, summary)
        if existing_date:
            return existing_date

        # 3. URL에서 날짜 추출 시도 (확장된 패턴)
        if url:
            url_date = self._extract_date_from_url_enhanced(url)
            if url_date:
                logger.debug(f"URL에서 날짜 추출 성공: {url_date}")
                return url_date

        # 4. 최신성 키워드 기반 날짜 추론
        recent_keywords = {
            "today": 0,
            "yesterday": 1,
            "hours ago": 0,
            "hour ago": 0,
            "minutes ago": 0,
            "minute ago": 0,
            "just now": 0,
            "breaking": 0,
            "latest": 0,
            "오늘": 0,
            "어제": 1,
            "방금": 0,
            "최신": 0,
            "속보": 0,
        }

        text_lower = text.lower()
        for keyword, days_ago in recent_keywords.items():
            if keyword in text_lower:
                extracted_date = (
                    datetime.datetime.now() - datetime.timedelta(days=days_ago)
                ).strftime("%Y-%m-%d")
                logger.debug(
                    f"최신 키워드에서 날짜 추론: {keyword} -> {extracted_date}"
                )
                return extracted_date

        # 5. 상대적 시간 표현 처리 (확장된 패턴)
        relative_patterns = [
            (
                r"(\d+)\s+hours? ago",
                lambda m: (
                    datetime.datetime.now() - datetime.timedelta(hours=int(m.group(1)))
                ),
            ),
            (
                r"(\d+)\s+days? ago",
                lambda m: (
                    datetime.datetime.now() - datetime.timedelta(days=int(m.group(1)))
                ),
            ),
            (
                r"(\d+)\s+minutes? ago",
                lambda m: (
                    datetime.datetime.now()
                    - datetime.timedelta(minutes=int(m.group(1)))
                ),
            ),
            (
                r"(\d+)\s+시간? 전",
                lambda m: (
                    datetime.datetime.now() - datetime.timedelta(hours=int(m.group(1)))
                ),
            ),
            (
                r"(\d+)\s+일? 전",
                lambda m: (
                    datetime.datetime.now() - datetime.timedelta(days=int(m.group(1)))
                ),
            ),
            (
                r"(\d+)\s+분? 전",
                lambda m: (
                    datetime.datetime.now()
                    - datetime.timedelta(minutes=int(m.group(1)))
                ),
            ),
        ]

        for pattern, date_func in relative_patterns:
            match = re.search(pattern, text_lower)
            if match:
                try:
                    date_obj = date_func(match)
                    extracted_date = date_obj.strftime("%Y-%m-%d")
                    logger.debug(
                        f"상대적 시간 표현에서 날짜 추출: {match.group(0)} -> {extracted_date}"
                    )
                    return extracted_date
                except Exception as e:
                    logger.warning(f"상대적 시간 처리 실패: {str(e)}")
                    continue

        # 6. 뉴스 사이트별 특수 날짜 패턴 (대폭 확장)
        news_site_patterns = [
            # TechCrunch
            r"techcrunch\.com/(\d{4})/(\d{2})/(\d{2})/",
            r"techcrunch\.com/(\d{4})/(\d{2})/(\d{2})",
            # The Verge
            r"theverge\.com/(\d{4})/(\d{2})/(\d{2})/",
            r"theverge\.com/(\d{4})/(\d{2})/(\d{2})",
            # VentureBeat
            r"venturebeat\.com/(\d{4})/(\d{2})/(\d{2})/",
            r"venturebeat\.com/(\d{4})/(\d{2})/(\d{2})",
            # Ars Technica
            r"arstechnica\.com/(\d{4})/(\d{2})/(\d{2})/",
            r"arstechnica\.com/(\d{4})/(\d{2})/(\d{2})",
            # Engadget
            r"engadget\.com/(\d{4})/(\d{2})/(\d{2})/",
            r"engadget\.com/(\d{4})/(\d{2})/(\d{2})",
            # 추가 AI 뉴스 사이트
            r"ai-news\.com/(\d{4})/(\d{2})/(\d{2})/",
            r"maginative\.com/(\d{4})/(\d{2})/(\d{2})/",
            r"linkedin\.com/pulse/.*?(\d{4})",
            r"zdnet\.com/(\d{4})/(\d{2})/(\d{2})/",
            r"cnet\.com/(\d{4})/(\d{2})/(\d{2})/",
            r"wired\.com/(\d{4})/(\d{2})/(\d{2})/",
            r"mashable\.com/(\d{4})/(\d{2})/(\d{2})/",
        ]

        if url:
            for pattern in news_site_patterns:
                match = re.search(pattern, url.lower())
                if match:
                    try:
                        groups = match.groups()
                        if len(groups) >= 3:
                            year, month, day = groups[:3]
                        elif len(groups) == 1:
                            # LinkedIn 등 연도만 있는 경우
                            year = groups[0]
                            # 현재 날짜에서 연도만 교체
                            current_date = datetime.datetime.now()
                            month = current_date.month
                            day = current_date.day
                        else:
                            continue

                        extracted_date = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
                        logger.debug(f"뉴스 사이트 URL에서 날짜 추출: {extracted_date}")
                        return extracted_date
                    except Exception as e:
                        logger.warning(f"뉴스 사이트 날짜 추출 실패: {str(e)}")
                        continue

        return None

    def _extract_date_from_page_content(
        self, title: str, summary: str
    ) -> Optional[str]:
        """페이지 내용에서 날짜 추출 (강화된 버전)"""
        text = f"{title} {summary}"

        # 1. 명시적인 날짜 패턴 (가장 신뢰도 높음)
        explicit_date_patterns = [
            # "January 9, 2025" 형식 (전체 월명 지원)
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}",
            # "Jan 9, 2025" 형식 (약어 월명)
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
            # "9 January 2025" 형식 (전체 월명)
            r"\d{1,2}\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}",
            # "9 Jan 2025" 형식 (약어 월명)
            r"\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}",
            # "2025년 1월 9일" 형식
            r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일",
            # "2025-01-09" 형식
            r"\d{4}[-/]\d{1,2}[-/]\d{1,2}",
            # "Posted on January 9, 2025" 형식 (전체 월명)
            r"(?:Posted|Published|Updated|Released|Updated|Created)\s+(?:on\s+)?(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}",
            # "Posted on Jan 9, 2025" 형식 (약어 월명)
            r"(?:Posted|Published|Updated|Released|Updated|Created)\s+(?:on\s+)?(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
            # "Posted on 2025-01-09" 형식
            r"(?:Posted|Published|Updated|Released|Updated|Created)\s+(?:on\s+)?\d{4}[-/]\d{1,2}[-/]\d{1,2}",
            # "January 9, 2025 •" 형식 (사용자 예시 특화)
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\s*•",
            # "Jan 9, 2025 •" 형식
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\s*•",
            # "• January 9, 2025" 형식
            r"•\s*(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}",
            # "• Jan 9, 2025" 형식
            r"•\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
        ]

        for pattern in explicit_date_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                for match in matches:
                    try:
                        if isinstance(match, tuple):
                            # 튜플인 경우 첫 요소 사용
                            date_str = match[0] if match[0] else str(match)
                        else:
                            date_str = str(match)

                        # 날짜 변환 시도
                        converted_date = self._convert_various_date_formats(date_str)
                        if converted_date:
                            # 유효성 검사 - 너무 오래된 날짜 필터링
                            date_obj = datetime.datetime.strptime(
                                converted_date, "%Y-%m-%d"
                            )
                            days_old = (datetime.datetime.now() - date_obj).days

                            # 180일 이상 된 날짜는 무시 (6개월 이상 된 기사 방지)
                            if days_old <= 180:
                                logger.debug(
                                    f"페이지 내용에서 명시적 날짜 추출 성공: {date_str} -> {converted_date}"
                                )
                                return converted_date
                            else:
                                logger.debug(
                                    f"페이지 내용 날짜가 너무 오래됨: {converted_date} ({days_old}일 전)"
                                )
                                continue
                    except Exception as e:
                        logger.debug(f"날짜 변환 실패: {match} - {str(e)}")
                        continue

        # 추가: 전체 텍스트에서 직접 날짜 패턴 검색 (findall 실패 대비)
        direct_patterns = [
            # "November 26, 2025" 형식
            r"(November|December|October|September|August|July|June|May|April|March|February|January)\s+\d{1,2},\s+\d{4}",
            # "Nov 26, 2025" 형식
            r"(Nov|Dec|Oct|Sep|Aug|Jul|Jun|May|Apr|Mar|Feb|Jan)\s+\d{1,2},\s+\d{4}",
        ]

        for pattern in direct_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    date_str = match.group(0)
                    converted_date = self._convert_various_date_formats(date_str)
                    if converted_date:
                        # 유효성 검사
                        date_obj = datetime.datetime.strptime(
                            converted_date, "%Y-%m-%d"
                        )
                        days_old = (datetime.datetime.now() - date_obj).days

                        if days_old <= 180:
                            logger.debug(
                                f"직접 패턴에서 날짜 추출 성공: {date_str} -> {converted_date}"
                            )
                            return converted_date
                except Exception as e:
                    logger.debug(f"직접 패턴 날짜 변환 실패: {date_str} - {str(e)}")
                    continue

        # 2. 저자 정보와 함께 있는 날짜 패턴
        author_date_patterns = [
            # "Chris McKay • January 9, 2025" 형식 (강화)
            r"[A-Za-z\s]+•\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
            # "Chris McKay\nJanuary 9, 2025" 형식 (줄바꿈 포함)
            r"[A-Za-z\s]+\n\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
            # "By Chris McKay, January 9, 2025" 형식
            r"By\s+[A-Za-z\s]+,\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
            # "Chris McKay January 9, 2025" 형식
            r"[A-Za-z\s]+\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
            # "Chris McKay\nChris McKay\nJanuary 9, 2025" 형식 (중복 저자명)
            r"[A-Za-z\s]+\n[A-Za-z\s]+\n\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
        ]

        for pattern in author_date_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                for match in matches:
                    try:
                        if isinstance(match, tuple):
                            date_str = match[0] if match[0] else str(match)
                        else:
                            date_str = str(match)

                        # 날짜 변환 시도
                        converted_date = self._convert_various_date_formats(date_str)
                        if converted_date:
                            # 유효성 검사
                            date_obj = datetime.datetime.strptime(
                                converted_date, "%Y-%m-%d"
                            )
                            days_old = (datetime.datetime.now() - date_obj).days

                            if days_old <= 180:
                                logger.debug(
                                    f"저자 정보에서 날짜 추출 성공: {date_str} -> {converted_date}"
                                )
                                return converted_date
                    except Exception as e:
                        logger.debug(f"저자 날짜 변환 실패: {match} - {str(e)}")
                        continue

        # 3. LinkedIn 특수 패턴
        linkedin_patterns = [
            # "2024년 2월 17일" 형식 (LinkedIn 한국어) - 강화
            r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일",
            # "Feb 17, 2024" 형식
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
            # "팔로워 6,045명\n\n2024년 2월 17일" 형식 (LinkedIn 특수)
            r"팔로워\s*[\d,]+\s*명\s*\n\s*\d{4}년\s*\d{1,2}월\s*\d{1,2}일",
            # "2024년 2월 17일\nMehnav Marketing Agency" 형식
            r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일\s*\n\s*[A-Za-z\s]+",
            # "Feb 17, 2024\nMehnav Marketing Agency" 형식
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\s*\n\s*[A-Za-z\s]+",
        ]

        for pattern in linkedin_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            if matches:
                for match in matches:
                    try:
                        if isinstance(match, tuple):
                            date_str = match[0] if match[0] else str(match)
                        else:
                            date_str = str(match)

                        # 날짜 변환 시도
                        converted_date = self._convert_various_date_formats(date_str)
                        if converted_date:
                            # 유효성 검사
                            date_obj = datetime.datetime.strptime(
                                converted_date, "%Y-%m-%d"
                            )
                            days_old = (datetime.datetime.now() - date_obj).days

                            if days_old <= 180:
                                logger.debug(
                                    f"LinkedIn 패턴에서 날짜 추출 성공: {date_str} -> {converted_date}"
                                )
                                return converted_date
                    except Exception as e:
                        logger.debug(f"LinkedIn 날짜 변환 실패: {match} - {str(e)}")
                        continue

        return None

    def _convert_various_date_formats(self, date_str: str) -> Optional[str]:
        """다양한 날짜 형식을 YYYY-MM-DD로 변환 (강화된 버전)"""
        if not date_str:
            return None

        try:
            # 전처리: 불필요한 문자 제거
            cleaned_date = date_str.strip()
            cleaned_date = re.sub(
                r"\s+", " ", cleaned_date
            )  # 다중 공백을 단일 공백으로

            # 1. "January 9, 2025" 형식 (전체 월명)
            month_day_year_full = re.match(
                r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})",
                cleaned_date,
                re.IGNORECASE,
            )
            if month_day_year_full:
                month, day, year = month_day_year_full.groups()
                month_num = datetime.datetime.strptime(month[:3], "%b").month
                return f"{year}-{month_num:02d}-{day.zfill(2)}"

            # 2. "Jan 9, 2025" 형식 (약어 월명)
            month_day_year = re.match(
                r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})",
                cleaned_date,
                re.IGNORECASE,
            )
            if month_day_year:
                month, day, year = month_day_year.groups()
                month_num = datetime.datetime.strptime(month[:3], "%b").month
                return f"{year}-{month_num:02d}-{day.zfill(2)}"

            # 3. "9 January 2025" 형식 (전체 월명)
            day_month_year_full = re.match(
                r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
                cleaned_date,
                re.IGNORECASE,
            )
            if day_month_year_full:
                day, month, year = day_month_year_full.groups()
                month_num = datetime.datetime.strptime(month[:3], "%b").month
                return f"{year}-{month_num:02d}-{day.zfill(2)}"

            # 4. "9 Jan 2025" 형식 (약어 월명)
            day_month_year = re.match(
                r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{4})",
                cleaned_date,
                re.IGNORECASE,
            )
            if day_month_year:
                day, month, year = day_month_year.groups()
                month_num = datetime.datetime.strptime(month[:3], "%b").month
                return f"{year}-{month_num:02d}-{day.zfill(2)}"

            # 3. "2025년 1월 9일" 형식 (강화)
            korean_format = re.match(
                r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", cleaned_date
            )
            if korean_format:
                year, month, day = korean_format.groups()
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

            # 4. "2025-01-09" 형식
            iso_format = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", cleaned_date)
            if iso_format:
                year, month, day = iso_format.groups()
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

            # 5. "2025/01/09" 형식
            slash_format = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", cleaned_date)
            if slash_format:
                year, month, day = slash_format.groups()
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

            # 6. "Jan 9 2025" 형식 (콤마 없음)
            jan_no_comma = re.match(
                r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})\s+(\d{4})",
                cleaned_date,
                re.IGNORECASE,
            )
            if jan_no_comma:
                month, day, year = jan_no_comma.groups()
                month_num = datetime.datetime.strptime(month[:3], "%b").month
                return f"{year}-{month_num:02d}-{day.zfill(2)}"

            # 7. "9 Jan 2025" 형식 (콤마 없음)
            day_jan_no_comma = re.match(
                r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{4})",
                cleaned_date,
                re.IGNORECASE,
            )
            if day_jan_no_comma:
                day, month, year = day_jan_no_comma.groups()
                month_num = datetime.datetime.strptime(month[:3], "%b").month
                return f"{year}-{month_num:02d}-{day.zfill(2)}"

            # 8. "2025.01.09" 형식 (점 구분자)
            dot_format = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", cleaned_date)
            if dot_format:
                year, month, day = dot_format.groups()
                return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

        except Exception as e:
            logger.debug(f"날짜 형식 변환 실패: {date_str} - {str(e)}")

        return None

    def _extract_date_from_url(self, url: str) -> Optional[str]:
        """URL에서 날짜 추출"""
        if not url:
            return None

        try:
            # URL에서 날짜 패턴 추출
            date_patterns = [
                r"/(\d{4})/(\d{2})/(\d{2})/",
                r"/(\d{4})-(\d{2})-(\d{2})/",
                r"/(\d{4})(\d{2})(\d{2})/",
                r"(\d{4})/(\d{2})/(\d{2})",
                r"(\d{4})-(\d{2})-(\d{2})",
            ]

            for pattern in date_patterns:
                match = re.search(pattern, url)
                if match:
                    try:
                        if len(match.groups()) == 3:
                            year, month, day = match.groups()
                            extracted_date = f"{year}-{month.zfill(2)}-{day.zfill(2)}"

                            # 유효한 날짜인지 확인
                            datetime.datetime.strptime(extracted_date, "%Y-%m-%d")
                            return extracted_date
                    except Exception as e:
                        logger.debug(f"URL 날짜 변환 실패: {str(e)}")
                        continue

            return None

        except Exception as e:
            logger.error(f"URL 날짜 추출 오류: {str(e)}")
            return None

    def _extract_date_from_url_enhanced(self, url: str) -> Optional[str]:
        """URL에서 날짜 추출 (강화된 버전)"""
        if not url:
            return None

        try:
            # 확장된 URL 날짜 패턴 추출
            date_patterns = [
                # 표준 형식
                r"/(\d{4})/(\d{2})/(\d{2})/",
                r"/(\d{4})-(\d{2})-(\d{2})/",
                r"/(\d{4})(\d{2})(\d{2})/",
                r"(\d{4})/(\d{2})/(\d{2})",
                r"(\d{4})-(\d{2})-(\d{2})",
                # 파일명에 날짜가 포함된 경우
                r"/(\d{4})-(\d{2})-(\d{2})-",
                r"/(\d{4})_(\d{2})_(\d{2})_",
                r"-(\d{4})-(\d{2})-(\d{2})\.",
                r"_(\d{4})_(\d{2})_(\d{2})\.",
                # LinkedIn 특수 패턴
                r"linkedin\.com/pulse/.*?(\d{4})",
                # Maginative 특수 패턴
                r"maginative\.com/article/.*?(\d{4})",
                # 다양한 구분자 패턴
                r"(\d{4})[._-](\d{2})[._-](\d{2})",
            ]

            for pattern in date_patterns:
                match = re.search(pattern, url.lower())
                if match:
                    try:
                        groups = match.groups()
                        if len(groups) >= 3:
                            year, month, day = groups[:3]
                        elif len(groups) == 1:
                            # LinkedIn 등 연도만 있는 경우
                            year = groups[0]
                            # 현재 날짜에서 연도만 교체
                            current_date = datetime.datetime.now()
                            month = current_date.month
                            day = current_date.day
                        else:
                            continue

                        extracted_date = f"{year}-{month.zfill(2)}-{day.zfill(2)}"

                        # 유효한 날짜인지 확인 및 너무 오래된 날짜 필터링
                        date_obj = datetime.datetime.strptime(
                            extracted_date, "%Y-%m-%d"
                        )
                        days_old = (datetime.datetime.now() - date_obj).days

                        # 365일 이상 된 날짜는 무시 (오래된 기사 방지)
                        if days_old > 365:
                            logger.debug(
                                f"URL에서 추출된 날짜가 너무 오래됨: {extracted_date} ({days_old}일 전)"
                            )
                            continue

                        logger.debug(f"URL에서 날짜 추출 성공: {extracted_date}")
                        return extracted_date
                    except Exception as e:
                        logger.debug(f"URL 날짜 변환 실패: {str(e)}")
                        continue

            return None

        except Exception as e:
            logger.error(f"URL 날짜 추출 오류: {str(e)}")
            return None

    def _detect_language(self, text: str) -> str:
        """텍스트 언어 감지 (개선된 정확도)"""
        if not text or not text.strip():
            return "en"

        text = text.strip()
        text_length = len(text)

        if text_length < 3:
            return "en"  # 너무 짧은 텍스트는 영어로 간주

        # 각 언어의 문자 카운트
        chinese_chars = len([char for char in text if "\u4e00" <= char <= "\u9fff"])
        japanese_chars = len(
            [
                char
                for char in text
                if ("\u3040" <= char <= "\u309f")  # 히라가나
                or ("\u30a0" <= char <= "\u30ff")  # 카타카나
                or ("\u4e00" <= char <= "\u9fff")
            ]
        )  # 한자(일본어)
        korean_chars = len([char for char in text if "\uac00" <= char <= "\ud7af"])

        # 라틴 문자(영어 등) 카운트
        latin_chars = len([char for char in text if "a" <= char.lower() <= "z"])

        # 각 언어의 비율 계산
        chinese_ratio = chinese_chars / text_length
        japanese_ratio = japanese_chars / text_length
        korean_ratio = korean_chars / text_length
        latin_ratio = latin_chars / text_length

        # 임계값 설정 (더 엄격한 기준)
        CHINESE_THRESHOLD = 0.15  # 15% 이상
        JAPANESE_THRESHOLD = 0.15  # 15% 이상
        KOREAN_THRESHOLD = 0.20  # 20% 이상
        LATIN_THRESHOLD = 0.70  # 70% 이상

        # 언어 판별 로직 (우선순위: 한국어 > 중국어 > 일본어 > 영어)
        if korean_ratio >= KOREAN_THRESHOLD:
            logger.debug(
                f"한국어 감지: {korean_ratio:.2f} ({korean_chars}/{text_length})"
            )
            return "ko"
        elif chinese_ratio >= CHINESE_THRESHOLD and chinese_ratio > japanese_ratio:
            logger.debug(
                f"중국어 감지: {chinese_ratio:.2f} ({chinese_chars}/{text_length})"
            )
            return "zh"
        elif japanese_ratio >= JAPANESE_THRESHOLD:
            logger.debug(
                f"일본어 감지: {japanese_ratio:.2f} ({japanese_chars}/{text_length})"
            )
            return "ja"
        elif latin_ratio >= LATIN_THRESHOLD:
            logger.debug(
                f"영어(라틴) 감지: {latin_ratio:.2f} ({latin_chars}/{text_length})"
            )
            return "en"
        else:
            # 혼합 언어나 기타 언어의 경우
            logger.debug(
                f"혼합/기타 언어: ko={korean_ratio:.2f}, zh={chinese_ratio:.2f}, ja={japanese_ratio:.2f}, en={latin_ratio:.2f}"
            )
            # 가장 높은 비율의 언어 반환
            ratios = [
                ("ko", korean_ratio),
                ("zh", chinese_ratio),
                ("ja", japanese_ratio),
                ("en", latin_ratio),
            ]
            dominant_lang = max(ratios, key=lambda x: x[1])
            return (
                dominant_lang[0] if dominant_lang[1] > 0.05 else "en"
            )  # 5% 미만이면 영어로 간주

    def _convert_to_english_query(self, query: str) -> str:
        """한국어 검색어를 영어 검색어로 변환"""
        # 한국어 AI 관련 키워드를 영어로 매핑
        korean_to_english = {
            "대규모 언어모델": "large language model LLM",
            "오픈AI": "OpenAI",
            "앤스로픽": "Anthropic",
            "구글": "Google",
            "딥시크": "DeepSeek",
            "알리바바": "Alibaba",
            "메타": "Meta",
            "엔비디아": "NVIDIA",
            "마이크로소프트": "Microsoft",
            "파인튜닝": "fine tuning",
            "생성형 AI": "generative AI",
            "챗봇": "chatbot",
            "컴퓨팅": "computing",
            "클라우드": "cloud",
            "로봇": "robot",
            "자율주행": "autonomous driving",
            "의료": "medical",
            "교육": "education",
            "금융": "finance",
            "코딩": "coding",
            "디자인": "design",
            "콘텐츠 생성": "content generation",
            "스마트 기기": "smart devices",
            "보안": "security",
            "규제": "regulation",
            "기술": "technology",
        }

        # 키워드 변환
        english_query = query
        for korean, english in korean_to_english.items():
            if korean in query:
                english_query = english_query.replace(korean, english)

        # 이미 영어가 포함되어 있으면 그대로 사용
        if re.search(r"[a-zA-Z]", query):
            return query

        return english_query

    def _translate_with_google(self, text: str) -> str:
        """구글 무료 번역 API를 통한 한국어 번역"""
        if not text or not text.strip():
            return ""

        text = text.strip()

        # 이미 한국어인지 확인
        detected_lang = self._detect_language(text)
        if detected_lang == "ko":
            logger.debug(f"이미 한국어 텍스트: {text[:30]}...")
            return text

        logger.debug(f"구글 번역 시도 ({detected_lang} -> ko): {text[:50]}...")

        # 구글 무료 번역 API 사용
        try:
            # googletrans 라이브러리 임포트 시도
            try:
                from googletrans import Translator

                translator = Translator()
            except ImportError:
                logger.error(
                    "googletrans 라이브러리가 설치되지 않았습니다. 'pip install googletrans==4.0.0-rc1'을 실행하세요."
                )
                return text

            # 여러 번역 시도 (안정성 확보)
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    # 번역 실행
                    result = translator.translate(
                        text,
                        dest="ko",
                        src=detected_lang if detected_lang != "auto" else "auto",
                    )

                    if result and hasattr(result, "text") and result.text:
                        translated_text = result.text.strip()

                        # 번역된 텍스트가 원본과 다를 경우에만 반환
                        if translated_text != text and len(translated_text) > 0:
                            logger.debug(
                                f"구글 번역 성공 (시도 {attempt + 1}): {text[:30]}... -> {translated_text[:30]}..."
                            )
                            return translated_text
                        else:
                            logger.debug(
                                f"번역 결과가 원본과 동일하거나 비어있음 (시도 {attempt + 1})"
                            )
                            if attempt == max_retries - 1:
                                return text
                    else:
                        logger.warning(
                            f"번역 결과가 유효하지 않음 (시도 {attempt + 1})"
                        )

                except Exception as translate_error:
                    logger.warning(
                        f"구글 번역 시도 {attempt + 1} 실패: {str(translate_error)}"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(1.0)

            # 모든 시도 실패 시 원본 텍스트 반환
            logger.warning(
                f"구글 번역 모든 시도 실패, 원본 텍스트 반환: {text[:30]}..."
            )
            return text

        except Exception as e:
            logger.error(f"구글 번역 기능 치명적 오류: {str(e)}, 원본 텍스트 사용")
            return text

    def _fallback_translate(self, text: str) -> str:
        """대체 번역 방법 (개선된 버전)"""
        if not text or not text.strip():
            return ""

        text = text.strip()

        # 1단계: Papago 무료 번역 시도
        try:
            papago_url = "https://openapi.naver.com/v1/papago/n2mt"
            headers = {
                "X-Naver-Client-Id": NAVER_CLIENT_ID,
                "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
            }
            data = {"source": "en", "target": "ko", "text": text}

            response = self.session.post(
                papago_url, headers=headers, data=data, timeout=10
            )
            if response.status_code == 200:
                result = response.json()
                if "message" in result and "result" in result["message"]:
                    translated_text = result["message"]["result"]["translatedText"]
                    if translated_text and translated_text != text:
                        logger.debug(
                            f"Papago 번역 성공: {text[:30]}... -> {translated_text[:30]}..."
                        )
                        return translated_text
        except Exception as papago_error:
            logger.debug(f"Papago 번역 실패: {str(papago_error)}")

        # 2단계: 기술 용어 매핑 (확장된 버전)
        tech_terms = {
            # AI 기본 용어
            "AI": "인공지능",
            "artificial intelligence": "인공지능",
            "machine learning": "머신러닝",
            "deep learning": "딥러닝",
            "neural network": "신경망",
            "algorithm": "알고리즘",
            # 모델 관련
            "LLM": "대규모 언어모델",
            "large language model": "대규모 언어모델",
            "model": "모델",
            "transformer": "트랜스포머",
            "ChatGPT": "챗GPT",
            "GPT": "GPT",
            "Claude": "클로드",
            "Gemini": "제미나이",
            "DeepSeek": "딥시크",
            "Qwen": "취엔",
            "Llama": "라마",
            # 기업명
            "OpenAI": "오픈AI",
            "Anthropic": "앤스로픽",
            "Google": "구글",
            "Microsoft": "마이크로소프트",
            "Meta": "메타",
            "NVIDIA": "엔비디아",
            "Alibaba": "알리바바",
            "Apple": "애플",
            # 기술 관련
            "API": "API",
            "release": "출시",
            "launch": "런칭",
            "update": "업데이트",
            "new": "새로운",
            "feature": "기능",
            "performance": "성능",
            "technology": "기술",
            "innovation": "혁신",
            "breakthrough": "돌파",
            "advance": "발전",
            "improvement": "개선",
            # 개발 관련
            "coding": "코딩",
            "programming": "프로그래밍",
            "development": "개발",
            "framework": "프레임워크",
            "library": "라이브러리",
            "tool": "도구",
            "tutorial": "튜토리얼",
            "guide": "가이드",
            "documentation": "문서",
        }

        # 영어 기술 용어를 한국어로 변환
        translated = text
        for en_term, ko_term in tech_terms.items():
            translated = re.sub(
                r"\b" + re.escape(en_term) + r"\b",
                ko_term,
                translated,
                flags=re.IGNORECASE,
            )

        # 3단계: 부분 번역 시도 (단어 단위)
        if translated == text:
            words = text.split()
            translated_words = []
            for word in words:
                word_lower = word.lower().strip(".,!?")
                translated_word = tech_terms.get(word_lower, word)
                translated_words.append(translated_word)
            translated = " ".join(translated_words)

        # 번역 결과 확인
        if translated != text:
            logger.debug(f"대체 번역 성공: {text[:30]}... -> {translated[:30]}...")
            return translated
        else:
            logger.debug(f"모든 대체 번역 실패: {text[:30]}...")
            return text

    def _translate_text(self, text: str) -> str:
        """텍스트 번역 (기존 호환성 유지) - 구글 번역 위임"""
        return self._translate_with_google(text)

    def _calculate_relevance_score(self, article: SearchResult) -> float:
        """블로그 주제 적합도 점수 계산"""
        score = 0.0

        # 최신성 가중치
        try:
            article_date = datetime.datetime.strptime(article.publish_date, "%Y-%m-%d")
            days_ago = (datetime.datetime.now() - article_date).days
            if days_ago <= 3:
                score += 0.3
            elif days_ago <= 7:
                score += 0.2
        except:
            pass

        # 기술적 중요도 키워드 포함 여부
        tech_keywords = [
            "GPT",
            "Claude",
            "Gemini",
            "AI",
            "LLM",
            "딥러닝",
            "머신러닝",
            "OpenAI",
            "Google",
            "Microsoft",
        ]
        title_lower = article.title.lower()
        tech_count = sum(1 for kw in tech_keywords if kw.lower() in title_lower)
        score += min(tech_count * 0.1, 0.4)

        # 기업명 포함 여부
        companies = [
            "OpenAI",
            "Google",
            "Microsoft",
            "Meta",
            "Apple",
            "NVIDIA",
            "Samsung",
            "LG",
            "KT",
            "SK",
        ]
        company_count = sum(1 for comp in companies if comp.lower() in title_lower)
        score += min(company_count * 0.1, 0.3)

        return max(0.0, min(1.0, score))

    def _calculate_text_similarity(self, text1: str, text2: str) -> float:
        """텍스트 유사도 계산"""
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())

        if not words1 or not words2:
            return 0.0

        intersection = words1.intersection(words2)
        union = words1.union(words2)

        return len(intersection) / len(union)

    def _contains_recent_ai_keywords(self, title: str, summary: str) -> bool:
        """최신 AI 관련 키워드 포함 여부 확인"""
        if not title and not summary:
            return False

        text = f"{title} {summary}".lower()

        # 최신 AI 관련 키워드 목록 (최신 트렌드 중심)
        recent_ai_keywords = [
            # 2024-2025 최신 모델
            "gpt-4",
            "gpt-5",
            "claude-3.5",
            "claude-4",
            "gemini-2.0",
            "gemini-pro",
            "deepseek-v3",
            "qwen-2.5",
            "llama-3.3",
            "llama-4",
            "mistral-large",
            "yi-1.5",
            "grok-2",
            "sora-2",
            "midjourney-v7",
            "stable-diffusion-3",
            # 최신 기술 트렌드
            "multimodal",
            "vision",
            "agent",
            "autonomous",
            "reasoning",
            "cot",
            "rag",
            "retrieval",
            "embedding",
            "fine-tuning",
            "quantization",
            "moe",
            "mixture of experts",
            "transformer",
            "attention",
            # 최신 애플리케이션
            "copilot",
            "github",
            "cursor",
            "windsurf",
            "claude-code",
            "chatgpt-desktop",
            "openai-o1",
            "openai-o3",
            "perplexity",
            # 기업 최신 뉴스
            "openai",
            "anthropic",
            "google",
            "microsoft",
            "meta",
            "nvidia",
            "apple",
            "amazon",
            "tesla",
            "samsung",
            "lg",
            "sk",
            "kt",
            # 한국 AI 관련
            "네이버",
            "카카오",
            "쿠콘",
            "업스테이지",
            "리디",
            "모두의말뭉치",
            "하이퍼클로바",
            "퍼플렉스",
            "마인즈랩",
            # 최신 개발 도구
            "langchain",
            "llamaindex",
            "vector database",
            "chromadb",
            "pinecone",
            "weaviate",
            "milvus",
            "faiss",
            "huggingface",
            "transformers",
            # 최신 용어
            "ai 챗봇",
            "ai 비서",
            "ai 코딩",
            "ai 개발",
            "ai 생성",
            "ai 분석",
            "ai 예측",
            "ai 추천",
            "ai 번역",
            "ai 요약",
            # 시장/투자 관련
            "ai 투자",
            "ai 스타트업",
            "ai 펀딩",
            "ai 인수",
            "ai 합병",
            "ai 시장",
            "ai 경쟁",
            "ai 전쟁",
            "ai 레이스",
            # 규제/정책
            "ai 규제",
            "ai 법",
            "ai 윤리",
            "ai 안전",
            "ai 거버넌스",
            "ai 정책",
            "ai 표준",
            "ai 인증",
        ]

        # 최신 키워드가 하나라도 포함되어 있는지 확인
        for keyword in recent_ai_keywords:
            if keyword in text:
                logger.debug(f"최신 AI 키워드 발견: {keyword}")
                return True

        # 기본 AI 키워드 확인 (최신 키워드가 없는 경우)
        basic_ai_keywords = [
            "ai",
            "artificial intelligence",
            "machine learning",
            "deep learning",
            "neural network",
            "llm",
            "large language model",
            "chatbot",
            "gpt",
        ]

        # 기본 AI 키워드가 2개 이상 포함되어 있는지 확인
        basic_count = sum(1 for keyword in basic_ai_keywords if keyword in text)
        if basic_count >= 2:
            logger.debug(f"기본 AI 키워드 {basic_count}개 발견")
            return True

        logger.debug(f"최신 AI 키워드 없음 (기본 키워드: {basic_count}개)")
        return False


class EnhancedAIBlogSystem:
    """향상된 AI 블로그 시스템 메인 클래스"""

    def __init__(self):
        """초기화"""
        self.db_manager = DatabaseManager()
        self.cache_manager = CacheManager(self.db_manager)
        self.search_engine = SearchEngine(self.db_manager, self.cache_manager)
        self._lock = threading.Lock()

        # 데이터베이스 초기화
        if not self.db_manager.initialize_database():
            raise Exception("데이터베이스 초기화 실패")

        # 주기적 캐시 정리 스레드 시작
        self._start_cache_cleanup_thread()

    def _start_cache_cleanup_thread(self):
        """캐시 정리 스레드 시작"""

        def cleanup_worker():
            while True:
                try:
                    time.sleep(3600)  # 1시간마다 실행
                    self.cache_manager.cleanup_expired_cache()
                except Exception as e:
                    logger.error(f"캐시 정리 스레드 오류: {str(e)}")

        cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
        cleanup_thread.start()
        logger.info("캐시 정리 스레드 시작")

    def search_latest_ai_articles(self) -> List[SearchResult]:
        """최신 AI 기사 검색 (네이버 API 안정성 강화 버전)"""
        logger.info("최신 AI 기사 검색 시작...")

        all_results = []

        # 한국어 키워드로 네이버 검색
        korean_keywords = [kw for kw in SEARCH_KEYWORDS if re.search("[가-힣]", kw)]

        # 영어 키워드로 Tavily 검색
        english_keywords = [
            kw for kw in SEARCH_KEYWORDS if not re.search("[가-힣]", kw)
        ]

        # 네이버 API는 순차적으로 처리하여 속도 제한 준수
        logger.info(
            f"네이버 검색 시작 (한국어 키워드 {len(korean_keywords)}개): {korean_keywords[:5]}..."
        )

        # 네이버 검색 - 순차 처리로 속도 제한 준수
        for i, keyword in enumerate(korean_keywords[:8]):  # 8개로 제한하여 부하 감소
            try:
                logger.info(
                    f"네이버 검색 진행 ({i + 1}/{len(korean_keywords[:8])}): {keyword}"
                )
                results = self.search_engine.search_naver(keyword, 5)
                if results:
                    all_results.extend(results)
                    logger.info(f"네이버 검색 성공: {keyword} -> {len(results)}개 결과")
                else:
                    logger.warning(f"네이버 검색 결과 없음: {keyword}")

                # API 요청 간격 증가 (1초)
                time.sleep(1.0)

            except Exception as e:
                logger.error(f"네이버 검색 실패 ({keyword}): {str(e)}")
                continue

        # DuckDuckGo 검색 - 병렬 처리 (실패 시 Tavily 자동 대체)
        logger.info(
            f"DuckDuckGo 검색 시작 (영어 키워드 {len(english_keywords)}개): {english_keywords[:3]}..."
        )

        # DuckDuckGo는 병렬 처리 가능
        with ThreadPoolExecutor(max_workers=2) as executor:  # 워커 수 줄임
            duckduckgo_futures = []
            for i, keyword in enumerate(english_keywords[:6]):  # 6개로 제한
                future = executor.submit(
                    self.search_engine.search_duckduckgo, keyword, 5
                )
                duckduckgo_futures.append(future)
                time.sleep(0.8)  # 간격 단축

            # DuckDuckGo 결과 수집
            for future in as_completed(duckduckgo_futures):
                try:
                    results = future.result(timeout=45)  # 타임아웃 증가
                    if results:
                        all_results.extend(results)
                        logger.debug(f"DuckDuckGo 검색 결과 추가: {len(results)}개")
                except Exception as e:
                    logger.error(f"DuckDuckGo 검색 태스크 오류: {str(e)}")

        # 결과 정렬 (적합도 점수순)
        all_results.sort(key=lambda x: x.relevance_score, reverse=True)

        # 데이터베이스에 검색 결과 저장
        saved_count = self._save_search_results_to_database(all_results)

        if saved_count > 0:
            logger.info(f"✅ 데이터베이스 저장 성공: {saved_count}개 기사 저장됨")
        else:
            logger.warning(
                "⚠️ 저장된 기사가 없습니다. 검색 결과나 데이터베이스 상태를 확인하세요."
            )

        logger.info(
            f"총 검색 결과: {len(all_results)}개 (네이버: {sum(1 for r in all_results if r.source == 'Naver')}, DuckDuckGo: {sum(1 for r in all_results if r.source == 'DuckDuckGo')}, Tavily: {sum(1 for r in all_results if r.source == 'Tavily')})"
        )
        return all_results

    def _save_search_results_to_database(self, results: List[SearchResult]) -> int:
        """검색 결과를 데이터베이스에 저장"""
        if not results:
            logger.info("저장할 검색 결과가 없습니다.")
            return 0

        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()

                saved_count = 0
                duplicate_count = 0

                for result in results:
                    try:
                        # 중복 확인 (URL 기반)
                        cursor.execute(
                            "SELECT id FROM articles WHERE url = ?", (result.url,)
                        )
                        if cursor.fetchone():
                            duplicate_count += 1
                            logger.debug(f"중복된 기사 건너뜀: {result.url}")
                            continue

                        # 데이터 삽입
                        cursor.execute(
                            """
                            INSERT OR IGNORE INTO articles
                            (title, summary, url, source, publish_date, relevance_score, language, keywords)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                            (
                                result.title,
                                result.summary,
                                result.url,
                                result.source,
                                result.publish_date,
                                result.relevance_score,
                                result.language,
                                json.dumps(result.keywords)
                                if result.keywords
                                else None,
                            ),
                        )

                        if cursor.rowcount > 0:
                            saved_count += 1
                            logger.debug(f"기사 저장 성공: {result.title[:50]}...")

                    except Exception as e:
                        logger.error(f"기사 저장 중 오류 ({result.url}): {str(e)}")
                        continue

                conn.commit()

                logger.info(
                    f"검색 결과 데이터베이스 저장 완료: 저장된 {saved_count}개, 중복 {duplicate_count}개"
                )
                return saved_count

        except Exception as e:
            logger.error(f"검색 결과 데이터베이스 저장 실패: {str(e)}")
            return 0

    def search_custom_topic(self, topic: str) -> List[SearchResult]:
        """사용자 지정 주제로 검색"""
        logger.info(f"사용자 지정 주제 검색: {topic}")

        all_results = []

        # 네이버 검색
        naver_results = self.search_engine.search_naver(topic, display=10)
        all_results.extend(naver_results)

        # DuckDuckGo 검색 (실패 시 Tavily 자동 대체)
        duckduckgo_results = self.search_engine.search_duckduckgo(topic, num_results=10)
        all_results.extend(duckduckgo_results)

        # 결과 정렬
        all_results.sort(key=lambda x: x.relevance_score, reverse=True)

        # 데이터베이스에 검색 결과 저장
        self._save_search_results_to_database(all_results)

        logger.info(f"사용자 지정 주제 검색 결과: {len(all_results)}개")
        return all_results

    def get_excluded_sites(self) -> List[Dict[str, Any]]:
        """제외된 사이트 목록 가져오기"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, domain, url_pattern, reason, created_at, active
                    FROM excluded_sites
                    ORDER BY created_at DESC
                """)

                sites = []
                for row in cursor.fetchall():
                    sites.append(
                        {
                            "id": row[0],
                            "domain": row[1],
                            "url_pattern": row[2],
                            "reason": row[3],
                            "created_at": row[4],
                            "active": bool(row[5]) if row[5] is not None else True,
                        }
                    )

                return sites

        except Exception as e:
            logger.error(f"제외된 사이트 목록 로드 오류: {str(e)}")
            return []

    def get_excluded_pages(self) -> List[Dict[str, Any]]:
        """제외된 페이지 목록 가져오기"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, url, title, reason, created_at, active
                    FROM excluded_pages
                    ORDER BY created_at DESC
                """)

                pages = []
                for row in cursor.fetchall():
                    pages.append(
                        {
                            "id": row[0],
                            "url": row[1],
                            "title": row[2],
                            "reason": row[3],
                            "created_at": row[4],
                            "active": bool(row[5]) if row[5] is not None else True,
                        }
                    )

                return pages

        except Exception as e:
            logger.error(f"제외된 페이지 목록 로드 오류: {str(e)}")
            return []

    def add_excluded_site(self, domain: str, reason: str = "user_excluded") -> bool:
        """제외된 사이트 추가"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO excluded_sites (domain, reason, active) VALUES (?, ?, 1)",
                    (domain, reason),
                )
                conn.commit()
                logger.info(f"제외된 사이트 추가됨: {domain}")
                return True

        except Exception as e:
            logger.error(f"제외된 사이트 추가 오류: {str(e)}")
            return False

    def add_excluded_page(
        self, url: str, title: str = "", reason: str = "user_excluded"
    ) -> bool:
        """제외된 페이지 추가"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO excluded_pages (url, title, reason, active) VALUES (?, ?, ?, 1)",
                    (url, title, reason),
                )
                conn.commit()
                logger.info(f"제외된 페이지 추가됨: {url}")
                return True

        except Exception as e:
            logger.error(f"제외된 페이지 추가 오류: {str(e)}")
            return False

    def remove_excluded_site(self, domain: str) -> bool:
        """제외된 사이트 삭제 (완전 삭제)"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM excluded_sites WHERE domain = ?", (domain,))
                conn.commit()
                logger.info(f"제외된 사이트 완전 삭제됨: {domain}")
                return True

        except Exception as e:
            logger.error(f"제외된 사이트 삭제 오류: {str(e)}")
            return False

    def remove_excluded_page(self, url: str) -> bool:
        """제외된 페이지 삭제 (완전 삭제)"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM excluded_pages WHERE url = ?", (url,))
                conn.commit()
                logger.info(f"제외된 페이지 완전 삭제됨: {url}")
                return True

        except Exception as e:
            logger.error(f"제외된 페이지 삭제 오류: {str(e)}")
            return False

    def _delete_article(self, article_id: int) -> bool:
        """기사 삭제 (실제로는 비활성화 처리)"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()

                # 기사가 존재하는지 확인
                cursor.execute("SELECT id FROM articles WHERE id = ?", (article_id,))
                if not cursor.fetchone():
                    logger.warning(f"삭제할 기사를 찾을 수 없음: {article_id}")
                    return False

                # 실제로는 삭제하지 않고 제외된 페이지 테이블에 추가하여 필터링되도록 처리
                cursor.execute("SELECT url FROM articles WHERE id = ?", (article_id,))
                article_result = cursor.fetchone()
                if article_result:
                    article_url = article_result[0]

                    # 제외된 페이지에 추가
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO excluded_pages (url, title, reason, active)
                        VALUES (?, ?, ?, 1)
                    """,
                        (article_url, f"삭제된 기사 ID {article_id}", "user_deleted"),
                    )

                    logger.info(
                        f"기사를 제외 목록에 추가하여 필터링 처리: {article_id} -> {article_url}"
                    )

                conn.commit()
                return True

        except Exception as e:
            logger.error(f"기사 삭제 오류: {str(e)}")
            return False

    def get_database_articles(
        self, page: int = 1, per_page: int = 10, source_filter: str = ""
    ) -> Dict[str, Any]:
        """데이터베이스 전체 기사 목록 가져오기 (제외된 사이트 필터링 포함)"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()

                # 제외된 사이트 목록 가져오기
                cursor.execute("SELECT domain FROM excluded_sites WHERE active = 1")
                excluded_sites = [row[0] for row in cursor.fetchall()]
                excluded_sites_lower = [site.lower() for site in excluded_sites]

                # 제외된 페이지 목록 가져오기
                cursor.execute("SELECT url FROM excluded_pages WHERE active = 1")
                excluded_pages = [row[0] for row in cursor.fetchall()]

                logger.info(
                    f"제외된 사이트 필터링 적용: {len(excluded_sites)}개 사이트, {len(excluded_pages)}개 페이지"
                )

                # 소스 필터링 조건 추가
                source_filter_condition = ""
                if source_filter:
                    source_filter_condition = " AND source = ?"

                # 모든 기사 조회 (ID 필드 포함)
                cursor.execute(
                    f"""
                    SELECT id, title, summary, url, source, publish_date, relevance_score
                    FROM articles
                    WHERE 1=1{source_filter_condition}
                    ORDER BY publish_date DESC
                """,
                    (source_filter,) if source_filter else (),
                )

                all_articles = []
                for row in cursor.fetchall():
                    article_id = row[0]
                    article_url = row[3]

                    # 제외된 페이지 필터링
                    if any(
                        excluded_page == article_url for excluded_page in excluded_pages
                    ):
                        logger.debug(f"제외된 페이지와 일치하여 필터링: {article_url}")
                        continue

                    # 제외된 사이트 필터링
                    should_exclude = False
                    if excluded_sites:
                        try:
                            article_domain = urllib.parse.urlparse(
                                article_url
                            ).netloc.lower()
                            for excluded_site in excluded_sites_lower:
                                if "://" in excluded_site:
                                    try:
                                        exclude_domain = urllib.parse.urlparse(
                                            excluded_site
                                        ).netloc
                                    except:
                                        exclude_domain = excluded_site
                                else:
                                    exclude_domain = excluded_site.split("/")[0]

                                exclude_domain = exclude_domain.strip()

                                if exclude_domain and (
                                    article_domain == exclude_domain
                                    or article_domain.endswith("." + exclude_domain)
                                    or exclude_domain in article_domain
                                ):
                                    should_exclude = True
                                    logger.debug(
                                        f"제외된 도메인과 일치하여 필터링: {article_domain} (제외: {exclude_domain})"
                                    )
                                    break
                        except Exception as e:
                            logger.warning(f"도메인 필터링 중 오류: {str(e)}")

                    if not should_exclude:
                        all_articles.append(
                            {
                                "id": article_id,  # ID 필드 추가
                                "title": row[1],
                                "summary": row[2],
                                "url": row[3],
                                "source": row[4],
                                "publish_date": row[5],
                                "relevance_score": float(row[6]) if row[6] else 0.0,
                            }
                        )
                # 전체 기사 수 (필터링 후)
                total_count = len(all_articles)

                # 페이지네이션 적용
                offset = (page - 1) * per_page
                articles = all_articles[offset : offset + per_page]

                # 전체 페이지 수 계산
                total_pages = (total_count + per_page - 1) // per_page

                logger.info(
                    f"데이터베이스 기사 목록 로드: 전체 {total_count}개, 현재 페이지 {len(articles)}개 (제외된 사이트 필터링 적용)"
                )

                return {
                    "articles": articles,
                    "totalCount": total_count,
                    "currentPage": page,
                    "totalPages": total_pages,
                }

        except Exception as e:
            logger.error(f"데이터베이스 기사 목록 로드 오류: {str(e)}")
            return {
                "articles": [],
                "totalCount": 0,
                "currentPage": page,
                "totalPages": 0,
            }

    def search_database_articles(
        self,
        search_query: str,
        page: int = 1,
        per_page: int = 10,
        source_filter: str = "",
    ) -> Dict[str, Any]:
        """데이터베이스 기사를 제목으로 검색"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()

                # 제외된 사이트 목록 가져오기
                cursor.execute("SELECT domain FROM excluded_sites WHERE active = 1")
                excluded_sites = [row[0] for row in cursor.fetchall()]
                excluded_sites_lower = [site.lower() for site in excluded_sites]

                # 제외된 페이지 목록 가져오기
                cursor.execute("SELECT url FROM excluded_pages WHERE active = 1")
                excluded_pages = [row[0] for row in cursor.fetchall()]

                logger.info(
                    f"검색어: {search_query}, 소스 필터: {source_filter}, 제외된 사이트: {len(excluded_sites)}개, 제외된 페이지: {len(excluded_pages)}개"
                )

                # 제목으로 검색 (ID 필드 포함)
                search_pattern = f"%{search_query}%"
                query = """
                    SELECT id, title, summary, url, source, publish_date, relevance_score
                    FROM articles
                    WHERE title LIKE ?
                """
                params = [search_pattern]

                if source_filter:
                    query += " AND source = ?"
                    params.append(source_filter)

                query += " ORDER BY publish_date DESC"

                cursor.execute(query, params)

                all_articles = []
                for row in cursor.fetchall():
                    article_id = row[0]
                    article_url = row[3]

                    # 제외된 페이지 필터링
                    if any(
                        excluded_page == article_url for excluded_page in excluded_pages
                    ):
                        logger.debug(f"제외된 페이지와 일치하여 필터링: {article_url}")
                        continue

                    # 제외된 사이트 필터링
                    should_exclude = False
                    if excluded_sites:
                        try:
                            article_domain = urllib.parse.urlparse(
                                article_url
                            ).netloc.lower()
                            for excluded_site in excluded_sites_lower:
                                if "://" in excluded_site:
                                    try:
                                        exclude_domain = urllib.parse.urlparse(
                                            excluded_site
                                        ).netloc
                                    except:
                                        exclude_domain = excluded_site
                                else:
                                    exclude_domain = excluded_site.split("/")[0]

                                exclude_domain = exclude_domain.strip()

                                if exclude_domain and (
                                    article_domain == exclude_domain
                                    or article_domain.endswith("." + exclude_domain)
                                    or exclude_domain in article_domain
                                ):
                                    should_exclude = True
                                    logger.debug(
                                        f"제외된 도메인과 일치하여 필터링: {article_domain} (제외: {exclude_domain})"
                                    )
                                    break
                        except Exception as e:
                            logger.warning(f"도메인 필터링 중 오류: {str(e)}")

                    if not should_exclude:
                        all_articles.append(
                            {
                                "id": article_id,
                                "title": row[1],
                                "summary": row[2],
                                "url": row[3],
                                "source": row[4],
                                "publish_date": row[5],
                                "relevance_score": float(row[6]) if row[6] else 0.0,
                            }
                        )

                # 전체 기사 수 (필터링 후)
                total_count = len(all_articles)

                # 페이지네이션 적용
                offset = (page - 1) * per_page
                articles = all_articles[offset : offset + per_page]

                # 전체 페이지 수 계산
                total_pages = (total_count + per_page - 1) // per_page

                logger.info(
                    f"데이터베이스 검색 결과: 전체 {total_count}개, 현재 페이지 {len(articles)}개 (검색어: {search_query})"
                )

                return {
                    "articles": articles,
                    "totalCount": total_count,
                    "currentPage": page,
                    "totalPages": total_pages,
                    "searchQuery": search_query,
                }

        except Exception as e:
            logger.error(f"데이터베이스 기사 검색 오류: {str(e)}")
            return {
                "articles": [],
                "totalCount": 0,
                "currentPage": page,
                "totalPages": 0,
                "searchQuery": search_query,
            }


# Flask 라우트
# 템플릿 경로 설정
app.template_folder = "templates"

# 전역 시스템 인스턴스 (싱글톤 패턴)
SYSTEM_INSTANCE = None


def get_system_instance():
    """시스템 인스턴스 가져오기 (싱글톤)"""
    global SYSTEM_INSTANCE
    if SYSTEM_INSTANCE is None:
        try:
            SYSTEM_INSTANCE = EnhancedAIBlogSystem()
            logger.info("새로운 시스템 인스턴스 생성됨")
        except Exception as e:
            logger.error(f"시스템 인스턴스 생성 실패: {str(e)}")
            raise e
    return SYSTEM_INSTANCE


@app.route("/")
def index():
    """메인 페이지"""
    return render_template("index.html")


@app.route("/api/search", methods=["POST"])
def search_articles():
    """최신 AI 기사 검색 API"""
    try:
        system = get_system_instance()
        results = system.search_latest_ai_articles()

        return jsonify(
            {
                "success": True,
                "results": [asdict(result) for result in results],
                "total": len(results),
            }
        )
    except Exception as e:
        logger.error(f"검색 API 오류: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/custom-search", methods=["POST"])
def custom_search():
    """사용자 지정 주제 검색 API"""
    try:
        data = request.get_json()
        topic = data.get("topic", "").strip()

        if not topic:
            return jsonify({"success": False, "error": "주제를 입력해주세요."}), 400

        system = get_system_instance()
        results = system.search_custom_topic(topic)

        return jsonify(
            {
                "success": True,
                "results": [asdict(result) for result in results],
                "total": len(results),
                "topic": topic,
            }
        )
    except Exception as e:
        logger.error(f"사용자 지정 검색 API 오류: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/excluded-sites", methods=["GET"])
def get_excluded_sites():
    """제외된 사이트 목록 API"""
    try:
        system = get_system_instance()
        sites = system.get_excluded_sites()

        return jsonify({"success": True, "sites": sites})
    except Exception as e:
        logger.error(f"제외된 사이트 목록 API 오류: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/excluded-pages", methods=["GET"])
def get_excluded_pages():
    """제외된 페이지 목록 API"""
    try:
        system = get_system_instance()
        pages = system.get_excluded_pages()

        return jsonify({"success": True, "pages": pages})
    except Exception as e:
        logger.error(f"제외된 페이지 목록 API 오류: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/database-articles", methods=["GET"])
def get_database_articles():
    """데이터베이스 전체 기사 목록 API (소스별 필터링 강화 버전)"""
    try:
        # 페이지네이션 파라미터
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("perPage", 10, type=int)
        search_query = request.args.get("search", "").strip()
        source_filter = request.args.get(
            "source", ""
        ).strip()  # 소스 필터링 파라미터 추가

        system = get_system_instance()

        if search_query:
            result = system.search_database_articles(
                search_query, page, per_page, source_filter
            )
        else:
            result = system.get_database_articles(page, per_page, source_filter)

        return jsonify({"success": True, **result})

    except Exception as e:
        logger.error(f"데이터베이스 기사 목록 API 오류: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/excluded-sites", methods=["POST"])
def add_excluded_site():
    """제외된 사이트 추가 API"""
    try:
        data = request.get_json()
        domain = data.get("domain", "").strip()
        reason = data.get("reason", "user_excluded")

        if not domain:
            return jsonify({"success": False, "error": "도메인을 입력해주세요."}), 400

        system = get_system_instance()
        success = system.add_excluded_site(domain, reason)

        if success:
            return jsonify(
                {
                    "success": True,
                    "message": f'도메인 "{domain}"이(가) 제외 목록에 추가되었습니다.',
                }
            )
        else:
            return jsonify(
                {"success": False, "error": "제외된 사이트 추가에 실패했습니다."}
            ), 500

    except Exception as e:
        logger.error(f"제외된 사이트 추가 API 오류: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/excluded-pages", methods=["POST"])
def add_excluded_page():
    """제외된 페이지 추가 API"""
    try:
        data = request.get_json()
        url = data.get("url", "").strip()
        title = data.get("title", "")
        reason = data.get("reason", "user_excluded")

        if not url:
            return jsonify({"success": False, "error": "URL을 입력해주세요."}), 400

        system = get_system_instance()
        success = system.add_excluded_page(url, title, reason)

        if success:
            return jsonify(
                {
                    "success": True,
                    "message": f'페이지 "{url}"이(가) 제외 목록에 추가되었습니다.',
                }
            )
        else:
            return jsonify(
                {"success": False, "error": "제외된 페이지 추가에 실패했습니다."}
            ), 500

    except Exception as e:
        logger.error(f"제외된 페이지 추가 API 오류: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/excluded-sites/<int:site_id>", methods=["DELETE"])
def remove_excluded_site(site_id):
    """제외된 사이트 삭제 API"""
    try:
        system = get_system_instance()

        # 도메인 조회
        with system.db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT domain FROM excluded_sites WHERE id = ?", (site_id,))
            result = cursor.fetchone()

            if not result:
                return jsonify(
                    {"success": False, "error": "해당 사이트를 찾을 수 없습니다."}
                ), 404

            domain = result[0]

        success = system.remove_excluded_site(domain)

        if success:
            return jsonify(
                {
                    "success": True,
                    "message": f'도메인 "{domain}"이(가) 제외 목록에서 완전 삭제되었습니다.',
                }
            )
        else:
            return jsonify(
                {"success": False, "error": "제외된 사이트 삭제에 실패했습니다."}
            ), 500

    except Exception as e:
        logger.error(f"제외된 사이트 삭제 API 오류: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/excluded-pages/<int:page_id>", methods=["DELETE"])
def remove_excluded_page(page_id):
    """제외된 페이지 삭제 API"""
    try:
        system = get_system_instance()

        # URL 조회
        with system.db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT url FROM excluded_pages WHERE id = ?", (page_id,))
            result = cursor.fetchone()

            if not result:
                return jsonify(
                    {"success": False, "error": "해당 페이지를 찾을 수 없습니다."}
                ), 404

            url = result[0]

        success = system.remove_excluded_page(url)

        if success:
            return jsonify(
                {
                    "success": True,
                    "message": f'페이지 "{url}"이(가) 제외 목록에서 완전 삭제되었습니다.',
                }
            )
        else:
            return jsonify(
                {"success": False, "error": "제외된 페이지 삭제에 실패했습니다."}
            ), 500

    except Exception as e:
        logger.error(f"제외된 페이지 삭제 API 오류: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/delete-article/<int:article_id>", methods=["DELETE"])
def delete_article(article_id):
    """기사 삭제 API"""
    try:
        system = get_system_instance()

        # 기사 URL 조회
        with system.db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT url FROM articles WHERE id = ?", (article_id,))
            result = cursor.fetchone()

            if not result:
                return jsonify(
                    {"success": False, "error": "해당 기사를 찾을 수 없습니다."}
                ), 404

            article_url = result[0]

        # 기사 삭제 (실제로는 비활성화)
        success = system._delete_article(article_id)

        if success:
            return jsonify(
                {
                    "success": True,
                    "message": f"기사 ID {article_id}이(가) 삭제되었습니다.",
                }
            )
        else:
            return jsonify(
                {"success": False, "error": "기사 삭제에 실패했습니다."}
            ), 500

    except Exception as e:
        logger.error(f"기사 삭제 API 오류: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/download/<filename>")
def download_file(filename):
    """파일 다운로드"""
    try:
        return send_file(filename, as_attachment=True)
    except Exception as e:
        logger.error(f"파일 다운로드 오류: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    # 시스템 초기화
    try:
        # 전역 인스턴스 초기화
        get_system_instance()
        logger.info("향상된 AI 블로그 시스템 초기화 완료")
    except Exception as e:
        logger.error(f"시스템 초기화 실패: {str(e)}")
        sys.exit(1)

    # 환경 변수에서 포트 번호 가져오기
    port = int(os.environ.get("PORT", 5001))

    # 프로덕션 서버 실행 (안정적인 백그라운드 실행)
    print(f"🌐 Flask 서버 시작: http://0.0.0.0:{port}")
    print(f"🔗 로컬 접속: http://localhost:{port}")
    print(f"📝 로그 파일 위치: {log_file}")
    print(f"📝 Flask 로그 위치: {flask_log_file}")
    print(f"📝 데이터베이스 파일: {DB_FILE}")

    # 안정적인 서버 실행 설정
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,  # 디버그 모드 비활성화 (안정성 향상)
        threaded=True,  # 멀티스레드 활성화
        use_reloader=False,  # 자동 리로더 비활성화
        passthrough_errors=False,  # 에러 통과 비활성화
    )
