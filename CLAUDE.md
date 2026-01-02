# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Flask-based web application that provides an AI news search and blog content generation system. It aggregates AI-related news articles from Naver Search API and helps users generate blog posts in Korean.

## Development Commands

```bash
# Activate virtual environment
source myenv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the Flask server (default port 5001)
python ainewssearch.py

# Run on custom port
PORT=8080 python ainewssearch.py
```

## Architecture

The entire application is contained in a single file ([ainewssearch.py](ainewssearch.py)) with the following key components:

### Data Classes
- `SearchResult`: Search result data model (title, summary, url, source, publish_date, keywords, relevance_score, language)
- `BlogTopic`: Blog topic data model
- `ExcludedSite`: Excluded domain data model
- `ExcludedPage`: Excluded page data model

### Core Classes

**DatabaseManager** (line 190)
- Manages SQLite database (`enhanced_ai_blog_database.db`)
- Implements schema versioning and migrations
- Thread-safe connection management with RLock
- Tables: `articles`, `excluded_sites`, `excluded_pages`, `search_cache`, `schema_version`

**CacheManager** (line 400)
- Manages search result caching with TTL (1 hour default)
- Uses MD5 hash for cache keys
- Filters cached results against exclusion lists

**SearchEngine** (line 602)
- Primary search method: `search_naver()` - Uses Naver Search API for Korean news
- HTML tag cleaning and result filtering
- Integrates with exclusion lists for sites/pages

**ContentGenerator** (line 3157)
- Generates blog content from search results

**EnhancedAIBlogSystem** (line 3348)
- Main orchestrator class that coordinates all components

### Flask REST API Endpoints

- `GET /` - Main index page
- `POST /api/search` - Search articles with predefined keywords
- `POST /api/custom-search` - Custom search with user-provided keywords
- `POST /api/generate-blog` - Generate blog content from selected articles
- `GET /api/excluded-sites` - List excluded domains
- `GET /api/excluded-pages` - List excluded pages
- `GET /api/database-articles` - List stored articles
- `POST /api/excluded-sites` - Add excluded site
- `POST /api/excluded-pages` - Add excluded page
- `DELETE /api/excluded-sites/<site_id>` - Remove excluded site
- `DELETE /api/excluded-pages/<page_id>` - Remove excluded page
- `POST /api/save-blog` - Save generated blog as markdown file
- `DELETE /api/delete-article/<article_id>` - Delete article from database
- `GET /api/download/<filename>` - Download generated file

## Configuration

Key constants defined at the top of [ainewssearch.py](ainewssearch.py:76-110):

- `DB_FILE`: Database filename (default: `enhanced_ai_blog_database.db`)
- `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET`: Naver API credentials
- `MAX_WORKERS`: Thread pool workers (default: 2)
- `REQUEST_TIMEOUT`: Request timeout in seconds (default: 45)
- `RATE_LIMIT_DELAY`: Delay between requests (default: 1.0s)
- `CACHE_TTL`: Cache time-to-live (default: 3600s)
- `SEARCH_KEYWORDS`: Predefined AI-related search keywords (Korean)
- `EXISTING_BLOG_TITLES`: List of existing blog titles for deduplication

## Logging

Two log files with rotation (10MB max, 5 backups):
- `ai_blog_system.log` - Main application logs
- `flask_server.log` - Flask server logs

## Notes

- The application runs with `debug=False` and `threaded=True` for production stability
- All database operations use context managers for proper connection handling
- The system automatically filters out excluded sites and pages from search results
