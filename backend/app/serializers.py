"""DRF serializers."""

from __future__ import annotations

import os

from django.conf import settings
from rest_framework import serializers

from app.models import Chunk, Document

DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {"application/pdf", "text/plain", "text/markdown"}
ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md"}


class DocumentSerializer(serializers.ModelSerializer):
    """List/read a document and accept an upload. ``file`` is write-only; the server derives
    ``content_type``/``size_bytes``/``original_filename`` from it and defaults ``title`` to the
    filename. Type + size are validated here; structural parsing is #11's job."""

    file = serializers.FileField(write_only=True)

    class Meta:
        model = Document
        fields = [
            "id",
            "title",
            "status",
            "error",
            "attempts",
            "content_type",
            "size_bytes",
            "original_filename",
            "created_at",
            "updated_at",
            "file",
        ]
        read_only_fields = [
            "id",
            "status",
            "error",
            "attempts",
            "content_type",
            "size_bytes",
            "original_filename",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {"title": {"required": False}}

    def validate_file(self, uploaded):
        max_bytes = getattr(settings, "TENANTIQ_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)
        if uploaded.size > max_bytes:
            raise serializers.ValidationError(
                f"File exceeds the maximum size of {max_bytes} bytes."
            )
        ext = os.path.splitext(uploaded.name)[1].lower()
        if uploaded.content_type not in ALLOWED_CONTENT_TYPES or ext not in ALLOWED_EXTENSIONS:
            raise serializers.ValidationError(
                "Unsupported file type. Allowed: PDF, plain text, Markdown."
            )
        return uploaded

    def create(self, validated_data):
        uploaded = validated_data["file"]
        validated_data.setdefault("title", uploaded.name)
        validated_data["original_filename"] = uploaded.name
        validated_data["content_type"] = uploaded.content_type or ""
        validated_data["size_bytes"] = uploaded.size
        return super().create(validated_data)


class CitedDocumentSerializer(serializers.ModelSerializer):
    """The document a citation points at — identity only.

    Deliberately two fields. This is nested inside every resolved citation, so anything added here is
    served on a path whose only job is showing one passage; ingestion state, size and filenames
    belong to the documents endpoints, where the caller asked for them.
    """

    class Meta:
        model = Document
        fields = ["id", "title"]
        read_only_fields = fields


class ChunkSerializer(serializers.ModelSerializer):
    """One retrieved chunk, resolved from a citation (#51, consumed by #19).

    ``text`` is the stored chunk verbatim — exactly ``source[start_offset:end_offset]`` of the
    extracted text (#45) — because this is the evidence a reader checks the answer against. The
    embedding is never serialized: it is internal, it is large, and it is the tenant's content in
    another form.
    """

    document = CitedDocumentSerializer(read_only=True)

    class Meta:
        model = Chunk
        fields = [
            "id",
            "index",
            "text",
            "char_count",
            "start_offset",
            "end_offset",
            "document",
        ]
        read_only_fields = fields
