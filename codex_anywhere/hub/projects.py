"""Group conversations by their node and project directory."""

from __future__ import annotations

import hashlib
import ntpath
import posixpath
import time

from .models import Project, db


def directory_key(host, cwd):
    windows = host.platform.lower().startswith("win") or bool(ntpath.splitdrive(cwd)[0])
    pathmod = ntpath if windows else posixpath
    normalized = pathmod.normpath(cwd)
    if windows:
        normalized = pathmod.normcase(normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def ensure_project(host, cwd, name=None):
    key = directory_key(host, cwd)
    project = Project.query.filter_by(host_id=host.id, cwd_key=key).first()
    if project is None:
        leaf = ntpath.basename(cwd.rstrip("/\\")) or posixpath.basename(cwd.rstrip("/")) or cwd
        project = Project(host_id=host.id, cwd=cwd, cwd_key=key, name=(name or leaf)[:240])
        db.session.add(project)
        db.session.flush()
    elif name:
        project.name = name[:240]
    project.updated_at = time.time()
    return project
