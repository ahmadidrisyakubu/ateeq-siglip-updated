import io
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from flask import abort, jsonify, request, send_file
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table as SqlTable,
    Text,
    and_,
    asc,
    case,
    create_engine,
    desc,
    func,
    insert,
    or_,
    select,
)
from werkzeug.exceptions import HTTPException


logger = logging.getLogger(__name__)
metadata = MetaData()

detections = SqlTable(
    "detections",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("result", String(16), nullable=False, index=True),
    Column("confidence", Float, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    Column("model_used", String(160), nullable=False, index=True),
    Column("original_filename", String(255), nullable=False),
    Column("file_type", String(100)),
    Column("file_size", Integer),
    Column("file_hash", String(64), index=True),
    Column("image_width", Integer),
    Column("image_height", Integer),
    Column("metadata_json", JSON),
    Column("client_ip", String(64)),
    Column("user_agent", Text),
)


def _database_url():
    url = os.environ.get("DATABASE_URL")
    if url:
        return url.replace("postgres://", "postgresql://", 1)

    data_dir = os.path.join(os.path.dirname(__file__), "instance")
    os.makedirs(data_dir, exist_ok=True)
    return f"sqlite:///{os.path.join(data_dir, 'detections.db')}"


def _create_engine():
    url = _database_url()
    options = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **options)


engine = _create_engine()


def initialize_database():
    try:
        metadata.create_all(engine)
        logger.info("Detection history database initialized")
    except Exception:
        logger.exception("Detection history database initialization failed")


def _serialize(record):
    item = dict(record)
    created_at = item.get("created_at")
    if created_at:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        item["created_at"] = created_at.isoformat()
    item["metadata"] = item.pop("metadata_json", None) or {}
    return item


def create_detection_record(
    *,
    result,
    confidence,
    model_used,
    original_filename,
    file_type,
    file_size,
    file_hash,
    image_width,
    image_height,
    extra_metadata=None,
):
    record_id = str(uuid.uuid4())
    values = {
        "id": record_id,
        "result": result.lower(),
        "confidence": float(confidence),
        "created_at": datetime.now(timezone.utc),
        "model_used": model_used,
        "original_filename": original_filename or "uploaded-image",
        "file_type": file_type,
        "file_size": file_size,
        "file_hash": file_hash,
        "image_width": image_width,
        "image_height": image_height,
        "metadata_json": extra_metadata or {},
        "client_ip": request.headers.get("X-Forwarded-For", request.remote_addr or "")[:64],
        "user_agent": request.user_agent.string[:1000],
    }
    with engine.begin() as connection:
        connection.execute(insert(detections).values(**values))
    return record_id


def _get_record(record_id):
    with engine.connect() as connection:
        row = connection.execute(
            select(detections).where(detections.c.id == record_id)
        ).mappings().first()
    return _serialize(row) if row else None


def history_api():
    try:
        page = max(request.args.get("page", 1, type=int), 1)
        per_page = min(max(request.args.get("per_page", 20, type=int), 1), 100)
        search = request.args.get("search", "").strip()
        result = request.args.get("result", "").strip().lower()
        model = request.args.get("model", "").strip()
        sort_by = request.args.get("sort_by", "created_at")
        sort_order = request.args.get("sort_order", "desc")

        filters = []
        if search:
            term = f"%{search}%"
            filters.append(
                or_(
                    detections.c.original_filename.ilike(term),
                    detections.c.file_hash.ilike(term),
                    detections.c.model_used.ilike(term),
                )
            )
        if result in {"real", "fake"}:
            filters.append(detections.c.result == result)
        if model:
            filters.append(detections.c.model_used == model)

        sort_columns = {
            "created_at": detections.c.created_at,
            "confidence": detections.c.confidence,
            "result": detections.c.result,
            "filename": detections.c.original_filename,
        }
        sort_column = sort_columns.get(sort_by, detections.c.created_at)
        ordering = asc(sort_column) if sort_order == "asc" else desc(sort_column)
        where_clause = and_(*filters) if filters else None

        query = select(detections)
        count_query = select(func.count()).select_from(detections)
        if where_clause is not None:
            query = query.where(where_clause)
            count_query = count_query.where(where_clause)

        with engine.connect() as connection:
            total = connection.execute(count_query).scalar_one()
            rows = connection.execute(
                query.order_by(ordering).limit(per_page).offset((page - 1) * per_page)
            ).mappings().all()
            models = connection.execute(
                select(detections.c.model_used).distinct().order_by(detections.c.model_used)
            ).scalars().all()

        return jsonify(
            {
                "records": [_serialize(row) for row in rows],
                "models": models,
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": max((total + per_page - 1) // per_page, 1),
            }
        )
    except Exception:
        logger.exception("Could not load detection history")
        return jsonify({"error": "Detection history is temporarily unavailable"}), 503


def history_detail_api(record_id):
    try:
        record = _get_record(record_id)
        if not record:
            return jsonify({"error": "Detection record not found"}), 404
        return jsonify(record)
    except Exception:
        logger.exception("Could not load detection record")
        return jsonify({"error": "Detection record is temporarily unavailable"}), 503


def statistics_api():
    try:
        cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
        day_expression = func.date(detections.c.created_at)

        with engine.connect() as connection:
            summary = connection.execute(
                select(
                    func.count().label("total"),
                    func.sum(case((detections.c.result == "real", 1), else_=0)).label("real"),
                    func.sum(case((detections.c.result == "fake", 1), else_=0)).label("fake"),
                    func.avg(detections.c.confidence).label("average_confidence"),
                )
            ).mappings().one()
            activity = connection.execute(
                select(day_expression.label("date"), func.count().label("count"))
                .where(detections.c.created_at >= cutoff)
                .group_by(day_expression)
                .order_by(day_expression)
            ).mappings().all()

        return jsonify(
            {
                "total": summary["total"] or 0,
                "real": summary["real"] or 0,
                "fake": summary["fake"] or 0,
                "average_confidence": round(float(summary["average_confidence"] or 0), 2),
                "recent_activity": [dict(row) for row in activity],
            }
        )
    except Exception:
        logger.exception("Could not load dashboard statistics")
        return jsonify({"error": "Dashboard statistics are temporarily unavailable"}), 503


def _report_rows(record):
    rows = [
        ["Detection Result", record["result"].title()],
        ["Confidence Score", f'{record["confidence"]:.2f}%'],
        ["Date and Time (UTC)", record["created_at"].replace("T", " ")],
        ["Model Used", record["model_used"]],
        ["Uploaded File", record["original_filename"]],
        ["File Type", record.get("file_type") or "Not available"],
        ["File Size", f'{record["file_size"]:,} bytes' if record.get("file_size") else "Not available"],
        ["Image Dimensions", f'{record["image_width"]} x {record["image_height"]} px'],
        ["SHA-256 Hash", record.get("file_hash") or "Not available"],
        ["Record ID", record["id"]],
    ]
    for key, value in record.get("metadata", {}).items():
        rows.append([key.replace("_", " ").title(), json.dumps(value) if isinstance(value, (dict, list)) else str(value)])
    styles = getSampleStyleSheet()
    return [[Paragraph(str(label), styles["BodyText"]), Paragraph(str(value), styles["BodyText"])] for label, value in rows]


def report_pdf(record_id, logo_path):
    try:
        record = _get_record(record_id)
        if not record:
            abort(404)

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=18 * mm,
            leftMargin=18 * mm,
            topMargin=16 * mm,
            bottomMargin=16 * mm,
            title="Deepfake Detection Report",
            author="Apex Broadcasting Network",
        )
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "ReportTitle",
            parent=styles["Title"],
            textColor=colors.HexColor("#1d4ed8"),
            alignment=TA_CENTER,
            spaceAfter=6,
        )
        story = []
        if os.path.exists(logo_path):
            story.extend([Image(logo_path, width=42 * mm, height=28 * mm), Spacer(1, 5)])
        story.extend(
            [
                Paragraph("Deepfake Detection Report", title_style),
                Paragraph("Apex Broadcasting Network", styles["Heading3"]),
                Spacer(1, 12),
                Paragraph(
                    "This report documents the result produced by the deployed AI image detection system.",
                    styles["BodyText"],
                ),
                Spacer(1, 14),
            ]
        )
        table = Table(_report_rows(record), colWidths=[48 * mm, 118 * mm], repeatRows=0)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e2e8f0")),
                    ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#0f172a")),
                    ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#94a3b8")),
                    ("ROWBACKGROUNDS", (1, 0), (1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        story.extend(
            [
                table,
                Spacer(1, 18),
                Paragraph(
                    "Note: AI detection results are probabilistic and should be considered alongside contextual and expert review.",
                    styles["Italic"],
                ),
            ]
        )
        doc.build(story)
        buffer.seek(0)
        return send_file(
            buffer,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"detection-report-{record_id[:8]}.pdf",
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Could not generate PDF report")
        abort(500)


def register_production_routes(app, logo_path):
    app.add_url_rule("/api/history", "history_api", history_api, methods=["GET"])
    app.add_url_rule("/api/history/<record_id>", "history_detail_api", history_detail_api, methods=["GET"])
    app.add_url_rule("/api/statistics", "statistics_api", statistics_api, methods=["GET"])
    app.add_url_rule(
        "/reports/<record_id>.pdf",
        "report_pdf",
        lambda record_id: report_pdf(record_id, logo_path),
        methods=["GET"],
    )
    initialize_database()
