import os
import re
import logging
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from github import fetch_repo_contents
from summarizer import summarize_repo

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="GitHub Repo Summarizer")

GITHUB_URL_PATTERN = re.compile(
    r"^https?://github\.com/([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+?)(?:\.git)?(?:/.*)?$"
)


class SummarizeRequest(BaseModel):
    github_url: str


@app.post("/summarize")
async def summarize(req: SummarizeRequest):
    match = GITHUB_URL_PATTERN.match(req.github_url.strip())
    if not match:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Invalid GitHub URL format. Expected: https://github.com/owner/repo"},
        )

    owner, repo = match.group(1), match.group(2)
    logger.info(f"Summarizing {owner}/{repo}")

    try:
        repo_data = await fetch_repo_contents(owner, repo)
    except ValueError as e:
        return JSONResponse(status_code=404, content={"status": "error", "message": str(e)})
    except PermissionError as e:
        return JSONResponse(status_code=403, content={"status": "error", "message": str(e)})
    except TimeoutError as e:
        return JSONResponse(status_code=504, content={"status": "error", "message": str(e)})
    except RuntimeError as e:
        msg = str(e)
        if "rate limit" in msg.lower():
            return JSONResponse(status_code=429, content={"status": "error", "message": msg})
        return JSONResponse(status_code=502, content={"status": "error", "message": msg})

    try:
        result = await summarize_repo(repo_data)
    except Exception as e:
        logger.exception("LLM summarization failed")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": f"Failed to generate summary: {str(e)}"},
        )

    return JSONResponse(status_code=200, content=result)
