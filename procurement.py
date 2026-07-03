"""Procurement Router — MySQL-backed AI evaluation endpoints only."""
from fastapi import APIRouter, HTTPException
import time

# Create router
router = APIRouter(
    prefix="/api/procurement",
    tags=["procurement"]
)

# ----- MySQL Configuration (Java team's database) -----

MYSQL_HOST = "192.168.0.46"
MYSQL_PORT = 3306
MYSQL_USER = "chennai_canvendor"
MYSQL_PASSWORD = "Can12345"
MYSQL_DB = "nfc_demo"

MIME_TO_EXT = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
}


def get_mysql_conn():
    import pymysql
    import pymysql.cursors
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        cursorclass=pymysql.cursors.DictCursor
    )


def ensure_ai_columns():
    """Add ai_score to proposal and ai_summary to rfq if they don't exist yet."""
    try:
        conn = get_mysql_conn()
        try:
            with conn.cursor() as cursor:
                for table, column in [("proposal", "ai_score"), ("rfq", "ai_summary")]:
                    cursor.execute(
                        "SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.COLUMNS "
                        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                        (MYSQL_DB, table, column),
                    )
                    if cursor.fetchone()["cnt"] == 0:
                        cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` LONGTEXT")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Non-fatal at startup


# Ensure columns exist when module loads
ensure_ai_columns()


# ─────────────────────────────────────────────────────────────────────────────
# AI Evaluation Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/rfq/{rfq_id}/evaluate")
async def evaluate_rfq(rfq_id: int):
    """
    Read RFQ + proposal documents from MySQL, run AI evaluation workflow,
    and write scores back to MySQL:
      - proposal.ai_score  (JSON per vendor)
      - rfq.ai_summary     (JSON overall)
    """
    import httpx
    import asyncio
    import json as _json

    AI_STUDIO_URL = "https://staging.canvendor.co.in/aistudioapi"
    WORKFLOW_ID = "7209a3a1-b2f1-4481-ab29-5ef4008235e4"
    API_KEY = "CAN@123"
    USER_ID = "7209a3a1-b2f1-4481-ab29-5ef4008235e4"

    # --- Ensure AI columns exist ---
    ensure_ai_columns()

    # --- Read documents from MySQL ---
    try:
        conn = get_mysql_conn()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MySQL connection failed: {str(e)}")

    try:
        with conn.cursor() as cursor:
            # RFQ document (rfq_id column set, proposal_id is NULL)
            cursor.execute(
                "SELECT document_base64, document_name, document_type FROM document WHERE rfq_id = %s LIMIT 1",
                (rfq_id,),
            )
            rfq_doc = cursor.fetchone()
            if not rfq_doc:
                raise HTTPException(status_code=404, detail=f"No document found for RFQ {rfq_id}")

            # All proposals for this RFQ (LEFT JOIN keeps proposals with no document)
            cursor.execute(
                """
                SELECT p.id AS proposal_id, p.vendor_name,
                       d.document_base64, d.document_name, d.document_type
                FROM proposal p
                LEFT JOIN document d ON d.proposal_id = p.id
                WHERE p.rfq_id = %s
                """,
                (rfq_id,),
            )
            all_proposals = cursor.fetchall()
    finally:
        conn.close()

    if not all_proposals:
        raise HTTPException(status_code=400, detail=f"No proposals found for RFQ {rfq_id}")

    # Only send proposals that have an uploaded document to the AI
    proposals = [p for p in all_proposals if p.get("document_base64")]

    if not proposals:
        raise HTTPException(status_code=400, detail=f"No proposals with documents found for RFQ {rfq_id}")

    # --- Build input payload ---
    def file_dict(filename, base64_data, mime_type):
        return {"filename": filename, "base64_data": base64_data, "mime_type": mime_type or "application/pdf"}

    rfq_ext = MIME_TO_EXT.get(rfq_doc.get("document_type"), ".pdf")
    rfq_filename = rfq_doc.get("document_name") or f"rfq{rfq_ext}"

    vendor_files = []
    proposal_id_map = {}  # workflow vendor_label -> proposal_id
    for p in proposals:
        ext = MIME_TO_EXT.get(p.get("document_type"), ".pdf")
        # Pass vendor_name as filename so the workflow maps vendor_label = vendor_name
        safe_filename = f"{p['vendor_name']}{ext}"
        # Mirror the workflow's label derivation: strip ext, replace _ and - with space
        vendor_label = p["vendor_name"].replace("_", " ").replace("-", " ").strip()
        proposal_id_map[vendor_label] = p["proposal_id"]
        vendor_files.append(file_dict(safe_filename, p["document_base64"], p.get("document_type")))

    input_payload = {
        "rfq_file": file_dict(rfq_filename, rfq_doc["document_base64"], rfq_doc.get("document_type")),
        "rfq_filename": rfq_filename,
        "vendor_files": vendor_files,
    }

    # --- Call AI workflow ---
    async with httpx.AsyncClient(timeout=30000.0) as client:
        try:
            exec_resp = await client.post(
                f"{AI_STUDIO_URL}/api/executions/execute",
                json={
                    "workflow_id": WORKFLOW_ID,
                    "input_data": {"input": _json.dumps(input_payload)},
                    "user_id": USER_ID,
                },
                headers={"X-API-Key": API_KEY},
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to reach AI service: {str(e)}")

        if exec_resp.status_code not in (200, 202):
            raise HTTPException(status_code=502, detail=f"AI service returned {exec_resp.status_code}: {exec_resp.text}")

        execution_id = exec_resp.json().get("execution_id")
        if not execution_id:
            raise HTTPException(status_code=502, detail="AI service did not return an execution ID")

        # --- Poll for completion (max 10 min) ---
        deadline = time.monotonic() + 600
        async with httpx.AsyncClient(timeout=30000.0) as poll_client:
            while time.monotonic() < deadline:
                await asyncio.sleep(2)
                try:
                    status_resp = await poll_client.get(
                        f"{AI_STUDIO_URL}/api/executions/{execution_id}",
                        headers={"X-API-Key": API_KEY},
                    )
                except Exception:
                    continue

                if status_resp.status_code != 200:
                    continue

                status_data = status_resp.json()
                exec_status = status_data.get("exc_status", "")

                if exec_status in ("failed", "cancelled"):
                    raise HTTPException(status_code=500, detail=f"AI evaluation failed: {status_data.get('error_message', 'Unknown error')}")

                if exec_status != "running":
                    output_data = status_data.get("output_data") or {}

                    # Extract result from known output keys
                    ai_result = (
                        output_data.get("final_response")
                        or output_data.get("response")
                        or output_data.get("result")
                        or output_data.get("output")
                        or output_data.get("recommendation")
                    )
                    if not ai_result:
                        for node_name, node_output in output_data.get("outputs", {}).items():
                            if node_name == "start" or not isinstance(node_output, dict):
                                continue
                            ai_result = (
                                node_output.get("response") or node_output.get("result")
                                or node_output.get("output") or node_output.get("recommendation")
                                or node_output.get("text") or node_output.get("message")
                            )
                            if ai_result:
                                break

                    if isinstance(ai_result, str):
                        try:
                            ai_result = _json.loads(ai_result)
                        except (_json.JSONDecodeError, ValueError):
                            pass

                    if not isinstance(ai_result, dict):
                        raise HTTPException(status_code=500, detail="AI evaluation returned an unrecognised result format")

                    # --- Parse new combined response: {"scores": [...], "report": {...}} ---
                    scores_list = ai_result.get("scores") or []   # vendor_loop output — reliable, all vendors
                    report_obj  = ai_result.get("report") or {}   # report_generator output — text fields only

                    # Fallback: if result_merger wasn't used and old format returned, handle gracefully
                    if not scores_list and (ai_result.get("category_breakdown") or ai_result.get("bar_chart")):
                        print("[evaluate_rfq] WARNING: old response format detected (no 'scores' key). Workflow may not have been updated yet.")

                    recommendation    = report_obj.get("recommendation") or ""
                    executive_summary = report_obj.get("executive_summary") or ""
                    vendor_summaries  = report_obj.get("vendor_summaries") or {}

                    print(f"[evaluate_rfq] scores_list count: {len(scores_list)}, report keys: {list(report_obj.keys())}")

                    # --- Build per-vendor score data directly from vendor_score_aggregator outputs ---
                    # Each item in scores_list has:
                    #   vendor_label, total_weighted_score, score_percentage,
                    #   category_scores: {"Technical": {raw_score, ...}, "Commercial": {...}, ...}
                    vendor_score_list = []
                    for entry in scores_list:
                        if isinstance(entry, dict) and "content" in entry:
                            entry = entry["content"]
                        if not isinstance(entry, dict):
                            continue
                        vendor_name = str(entry.get("vendor_label", "")).strip()
                        prop_id = proposal_id_map.get(vendor_name)
                        if not prop_id:
                            print(f"[evaluate_rfq] No proposal match for vendor_label={repr(vendor_name)}. All known keys: {list(proposal_id_map.keys())}")
                            continue

                        cat_scores = entry.get("category_scores") or {}

                        def _cat_raw(cat_key):
                            cat = cat_scores.get(cat_key) or {}
                            v = cat.get("raw_score")
                            return float(v) if v is not None else None

                        vendor_score_list.append({
                            "prop_id":              prop_id,
                            "vendor_name":          vendor_name,
                            "ai_score":             entry.get("total_weighted_score"),
                            "score_pct":            entry.get("score_percentage"),
                            "technical":            _cat_raw("Technical"),
                            "commercial":           _cat_raw("Commercial"),
                            "delivery":             _cat_raw("Delivery"),
                            "quality":              _cat_raw("Quality"),
                            "terms_and_conditions": _cat_raw("Terms and Conditions"),
                            "summary":              vendor_summaries.get(vendor_name) or executive_summary,
                        })

                    # Detect vendors sent to workflow but absent from scores_list
                    evaluated_prop_ids = {v["prop_id"] for v in vendor_score_list}
                    skipped_proposals = [p for p in proposals if p["proposal_id"] not in evaluated_prop_ids]
                    if skipped_proposals:
                        skipped_names = [p["vendor_name"] for p in skipped_proposals]
                        print(f"[evaluate_rfq] WARNING: {len(skipped_proposals)} vendor(s) missing from scores: {skipped_names}")

                    # Derive ranks in Python: sort by score_pct descending
                    vendor_score_list.sort(
                        key=lambda x: x["score_pct"] if x["score_pct"] is not None else -1,
                        reverse=True,
                    )
                    for rank_idx, v in enumerate(vendor_score_list, 1):
                        v["rank"] = rank_idx

                    # Build bar_chart in Python from reliable scores (top 4 for UI)
                    bar_chart_all = sorted(
                        [
                            {
                                "rank":      v["rank"],
                                "vendor":    v["vendor_name"],
                                "score":     round(float(v["ai_score"]), 3) if v["ai_score"] is not None else None,
                                "score_pct": round(float(v["score_pct"]), 1) if v["score_pct"] is not None else None,
                            }
                            for v in vendor_score_list
                        ],
                        key=lambda x: x["score_pct"] if x["score_pct"] is not None else -1,
                        reverse=True,
                    )
                    top_4_bar = bar_chart_all[:4]

                    try:
                        save_conn = get_mysql_conn()
                        try:
                            with save_conn.cursor() as cursor:
                                # Per-proposal: save ai_score JSON
                                for v in vendor_score_list:
                                    ai_score_json = _json.dumps({
                                        "ai_score":             v["ai_score"],
                                        "score_percentage":     v["score_pct"],
                                        "rank":                 v["rank"],
                                        "technical":            v["technical"],
                                        "commercial":           v["commercial"],
                                        "delivery":             v["delivery"],
                                        "quality":              v["quality"],
                                        "terms_and_conditions": v["terms_and_conditions"],
                                        "summary":              v["summary"],
                                    })
                                    cursor.execute(
                                        "UPDATE proposal SET ai_score = %s WHERE id = %s",
                                        (ai_score_json, v["prop_id"]),
                                    )

                                # Mark skipped vendors (edge case: workflow loop dropped them)
                                for sp in skipped_proposals:
                                    cursor.execute(
                                        "UPDATE proposal SET ai_score = %s WHERE id = %s",
                                        (_json.dumps({"ai_score": None, "score_percentage": None, "rank": None, "summary": "Not evaluated — workflow did not return a score for this vendor."}), sp["proposal_id"]),
                                    )

                                # RFQ-level: top 4 bar chart + recommendation + executive_summary
                                ai_summary_json = _json.dumps({
                                    "bar_chart": {
                                        "title":      "Vendor Overall Score Comparison",
                                        "chart_type": "bar",
                                        "x_axis":     "Vendor",
                                        "y_axis":     "Overall Score (out of 5)",
                                        "data":       top_4_bar,
                                    },
                                    "recommendation":    recommendation,
                                    "executive_summary": executive_summary,
                                })
                                cursor.execute(
                                    "UPDATE rfq SET ai_summary = %s WHERE id = %s",
                                    (ai_summary_json, rfq_id),
                                )
                            save_conn.commit()
                        finally:
                            save_conn.close()
                    except Exception as save_err:
                        print(f"[evaluate_rfq] MySQL save error (non-fatal): {save_err}")

                    return {"status": "success", "message": "Evaluation complete. Scores saved successfully."}

    raise HTTPException(status_code=504, detail="AI evaluation timed out after 10 minutes")


@router.get("/rfq/{rfq_id}/ai-summary")
async def get_rfq_ai_summary(rfq_id: int):
    """
    View Summary screen — returns bar_chart, recommendation, executive_summary
    stored in rfq.ai_summary after evaluation.
    """
    import json as _json

    try:
        conn = get_mysql_conn()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MySQL connection failed: {str(e)}")

    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT ai_summary FROM rfq WHERE id = %s", (rfq_id,))
            row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail=f"RFQ {rfq_id} not found")
    if not row.get("ai_summary"):
        raise HTTPException(status_code=404, detail=f"No AI summary for RFQ {rfq_id}. Run evaluation first.")

    try:
        summary = _json.loads(row["ai_summary"])
    except Exception:
        raise HTTPException(status_code=500, detail="Stored AI summary is malformed")

    return {"status": "success", "rfq_id": rfq_id, **summary}


@router.get("/rfq/{rfq_id}/proposal-scores")
async def get_rfq_proposal_scores(rfq_id: int):
    """
    View Proposal screen — returns AI score for every proposal under an RFQ,
    sorted by rank.
    """
    import json as _json

    try:
        conn = get_mysql_conn()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MySQL connection failed: {str(e)}")

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, vendor_name, ai_score FROM proposal WHERE rfq_id = %s ORDER BY id",
                (rfq_id,),
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        score_data = None
        if row.get("ai_score"):
            try:
                score_data = _json.loads(row["ai_score"])
            except Exception:
                pass
        result.append({
            "proposal_id": row["id"],
            "vendor_name": row["vendor_name"],
            "ai_score": score_data,
        })

    # Sort by rank (proposals with no score go last)
    result.sort(key=lambda x: (x["ai_score"] or {}).get("rank", 9999) if x["ai_score"] else 9999)

    return {"status": "success", "rfq_id": rfq_id, "proposals": result}


@router.get("/proposal/{proposal_id}/ai-score")
async def get_proposal_ai_score(proposal_id: int):
    """
    AI Summary modal — returns full score breakdown for a single proposal.
    """
    import json as _json

    try:
        conn = get_mysql_conn()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MySQL connection failed: {str(e)}")

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, vendor_name, ai_score FROM proposal WHERE id = %s",
                (proposal_id,),
            )
            row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")
    if not row.get("ai_score"):
        raise HTTPException(status_code=404, detail=f"No AI score for proposal {proposal_id}. Run evaluation first.")

    try:
        score_data = _json.loads(row["ai_score"])
    except Exception:
        raise HTTPException(status_code=500, detail="Stored AI score is malformed")

    return {
        "status": "success",
        "proposal_id": proposal_id,
        "vendor_name": row["vendor_name"],
        **score_data,
    }
