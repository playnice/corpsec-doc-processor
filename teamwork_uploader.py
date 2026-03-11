"""
Teamwork Uploader module.
Uploads renamed PDFs to Teamwork.com via their API.
Maps company names to project entities and sets categories/resolution dates.
"""

import logging
from pathlib import Path

import requests

import config
from ai_analyzer import DocumentMetadata

logger = logging.getLogger(__name__)

# Timeout for all HTTP requests (connect, read) in seconds
REQUEST_TIMEOUT = 60


class TeamworkUploader:
    """Client for uploading documents to Teamwork."""

    def __init__(self):
        self.base_url = config.TEAMWORK_BASE_URL.rstrip("/")
        self.api_key = config.TEAMWORK_API_KEY
        self.session = requests.Session()
        # Teamwork uses API key as username with 'x' as password for basic auth
        self.session.auth = (self.api_key, "x")
        self.session.headers.update({
            "Accept": "application/json",
        })

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upload_document(
        self,
        file_path: Path,
        metadata: DocumentMetadata,
        category_name: str | None = None,
    ) -> bool:
        """
        Upload a document to Teamwork under the matching project/entity.

        Steps:
            1. Find project by company name
            2. Upload the file to get a pending file ref
            3. Create a file entry in the project with category & date

        Returns True on success.
        """
        if not self.base_url or not self.api_key:
            logger.warning("Teamwork not configured — skipping upload.")
            return False

        # Step 1: Find the project for this company
        project_id = self._find_project_by_company(metadata.company_name or "")
        if not project_id:
            logger.error(
                "No Teamwork project found for company: %s", metadata.company_name
            )
            return False

        # Step 2: Upload file to get a pending file reference
        pending_file_ref = self._upload_file(file_path)
        if not pending_file_ref:
            return False

        # Step 3: Create file entry in the project
        category_id = None
        if category_name:
            category_id = self._find_or_create_category(project_id, category_name)

        success = self._create_file_entry(
            project_id=project_id,
            pending_file_ref=pending_file_ref,
            filename=file_path.name,
            description=metadata.document_type or "",
            category_id=category_id,
        )

        if success:
            logger.info("Uploaded to Teamwork: %s (project %s)", file_path.name, project_id)
        return success

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_project_by_company(self, company_name: str) -> str | None:
        """Search Teamwork projects for one matching the company name."""
        try:
            resp = self.session.get(
                f"{self.base_url}/projects.json",
                params={"status": "active"},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            projects = resp.json().get("projects", [])
        except requests.RequestException:
            logger.exception("Failed to fetch Teamwork projects")
            return None

        company_lower = company_name.lower()
        # Try exact-ish match first, then substring
        for project in projects:
            if company_lower in project.get("name", "").lower():
                return str(project["id"])

        # Also try the company short name
        short = config.get_company_short_name(company_name)
        for project in projects:
            if short.lower() in project.get("name", "").lower():
                return str(project["id"])

        return None

    def _upload_file(self, file_path: Path) -> str | None:
        """Upload a file and return the pending file reference."""
        try:
            with open(file_path, "rb") as f:
                resp = self.session.post(
                    f"{self.base_url}/pendingfiles.json",
                    files={"file": (file_path.name, f, "application/pdf")},
                    timeout=REQUEST_TIMEOUT,
                )
            resp.raise_for_status()
            ref = resp.json().get("pendingFile", {}).get("ref")
            if not ref:
                logger.error("No pending file ref in response: %s", resp.text[:200])
            return ref
        except requests.RequestException:
            logger.exception("Failed to upload file to Teamwork")
            return None

    def _find_or_create_category(self, project_id: str, category_name: str) -> str | None:
        """Find a file category by name in a project, or create it."""
        try:
            resp = self.session.get(
                f"{self.base_url}/projects/{project_id}/filecategories.json",
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            categories = resp.json().get("categories", [])
        except requests.RequestException:
            logger.exception("Failed to fetch file categories")
            return None

        for cat in categories:
            if cat.get("name", "").lower() == category_name.lower():
                return str(cat["id"])

        # Create new category
        try:
            resp = self.session.post(
                f"{self.base_url}/projects/{project_id}/filecategories.json",
                json={"category": {"name": category_name}},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return str(resp.json().get("categoryId", ""))
        except requests.RequestException:
            logger.exception("Failed to create file category")
            return None

    def _create_file_entry(
        self,
        project_id: str,
        pending_file_ref: str,
        filename: str,
        description: str,
        category_id: str | None,
    ) -> bool:
        """Create a file entry in the project with the uploaded file."""
        payload: dict = {
            "file": {
                "name": filename,
                "description": description,
                "pendingFileRef": pending_file_ref,
            }
        }
        if category_id:
            payload["file"]["categoryId"] = category_id

        try:
            resp = self.session.post(
                f"{self.base_url}/projects/{project_id}/files.json",
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException:
            logger.exception("Failed to create file entry in Teamwork")
            return False
