import logging
import os
import re
import json
import requests
from datetime import timedelta, datetime
from azure.identity import DefaultAzureCredential
from azure.monitor.query import LogsQueryClient
import azure.functions as func

app = func.FunctionApp()


# Normalize Ollama response into a predictable schema before writing incident data downstream.
# The Model may return valid JSON, JSON embedded in text, or slightly different key names.

def extract_json_from_ai_response(ai_text: str) -> dict:
    default_result = {
        "incident_summary": "",
        "likely_root_cause": "",
        "recommended_actions": "",
        "potential_impact": ""
    }

    if not ai_text or not ai_text.strip():
        return default_result

    try:
        parsed = json.loads(ai_text)
    except Exception:
        try:
            json_match = re.search(r"\{.*\}", ai_text, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group(0))
            else:
                return default_result
        except Exception:
            logging.warning("Unable to parse JSON from Ollama response.")
            return default_result

    key_aliases = {
        "incident_summary": "incident_summary",
        "summary": "incident_summary",
        "likely_root_cause": "likely_root_cause",
        "root_cause": "likely_root_cause",
        "root_cause_analysis": "likely_root_cause",
        "recommended_actions": "recommended_actions",
        "recommended_action": "recommended_actions",
        "recoverable_actions": "recommended_actions",
        "remediation_steps": "recommended_actions",
        "potential_impact": "potential_impact",
        "impact": "potential_impact"
    }

    normalized = {}

    for key, value in parsed.items():
        normalized_key = key.strip().lower().replace(" ", "_")
        target_key = key_aliases.get(normalized_key)

        if target_key:
            normalized[target_key] = value

    return {**default_result, **normalized}


# Remove Prompt Leakage, placeholder test, and formatting artifacts from AI-generated output.
# This keeps incident reports clean before storing them in a log Analytics or sending notifications.

def clean_ai_text(text: str) -> str:
    if not text:
        return ""

    blocked_phrases = [
        "write 2 clear sentences",
        "write 1 concise technical explanation",
        "write 3 specific actions",
        "write 1 complete sentence",
        "do not use markdown",
        "do not use bullet symbols",
        "replace this with",
        "<actual",
        "insert",
        "placeholder"
    ]

    lines = []
    for line in str(text).splitlines():
        clean_line = line.strip()
        clean_line = clean_line.replace("[Date] at [Time], ", "")
        clean_line = clean_line.replace("On [Date] at [Time], ", "")
        clean_line = clean_line.replace("[Date]", "the alert time")
        clean_line = clean_line.replace("[Time]", "the alert timestamp")
        if not clean_line:
            continue

        lower_line = clean_line.lower()
        if any(phrase in lower_line for phrase in blocked_phrases):
            continue

        lines.append(clean_line)

    return "\n".join(lines).strip()


# Suppress duplicate notification for the same alert/resource/status combination within a shot window.
# This prevents alert noise while still allowing new or changed incidents to notify operators.

def should_send_notification(workspace_id: str, alert_name: str, affected_resource: str, status: str) -> bool:
    try:
        credential = DefaultAzureCredential()
        client = LogsQueryClient(credential)

        safe_alert_name = alert_name.replace("'", "''")
        safe_resource = affected_resource.replace("'", "''")
        safe_status = status.replace("'", "''")

        query = f"""
        IncidentReports_CL
        | where TimeGenerated > ago(30m)
        | where AlertName == '{safe_alert_name}'
        | where AffectedResource == '{safe_resource}'
        | where Status == '{safe_status}'
        | summarize Count=count()
        """

        result = client.query_workspace(
            workspace_id,
            query,
            timespan=timedelta(minutes=30)
        )

        for table in result.tables:
            for row in table.rows:
                return row[0] == 0

        return True

    except Exception as e:
        logging.error("Notification dedupe check failed: %s", str(e))
        return True


# Map raw azure Monitor alert signals into operational categories that are easier for the AI model
# and Support engineers to reason about during incident triage

def get_alert_category(alert_name: str, resource_type: str, metric_name: str) -> str:
    alert_name_lower = alert_name.lower()
    metric_name_lower = metric_name.lower()
    resource_type_lower = resource_type.lower()

    if "cpu" in alert_name_lower or "cpu" in metric_name_lower:
        return "Virtual machine high CPU utilization"

    if "storage" in alert_name_lower or "transaction" in alert_name_lower or "storage" in resource_type_lower:
        return "Storage account transaction volume anomaly"

    if "function" in alert_name_lower or "function" in resource_type_lower:
        return "Function App execution or availability issue"

    return "Azure Monitor infrastructure alert"


# Normalize Azure Monitor alert payload fields into a consistent incident context.
# Alert payloads can vary by resource type, signal type, and monitoring service.

def build_incident_context(essentials: dict, alert_context: dict, affected_resource: str, resource_type: str) -> dict:
    condition = alert_context.get("condition", {})
    all_of = condition.get("allOf", [])

    metric_name = "Unknown"
    metric_value = "Unknown"
    threshold = "Unknown"
    operator = "Unknown"
    time_aggregation = "Unknown"

    if all_of and isinstance(all_of, list):
        first_condition = all_of[0]
        metric_name = first_condition.get("metricName", "Unknown")
        metric_value = first_condition.get("metricValue", "Unknown")
        threshold = first_condition.get("threshold", "Unknown")
        operator = first_condition.get("operator", "Unknown")
        time_aggregation = first_condition.get("timeAggregation", "Unknown")

    alert_name = essentials.get("alertRule", "Unknown Alert")
    severity = essentials.get("severity", "Unknown")
    status = essentials.get("monitorCondition", "Unknown")
    signal_type = essentials.get("signalType", "Unknown")
    monitoring_service = essentials.get("monitoringService", "Unknown")

    alert_category = get_alert_category(alert_name, resource_type, metric_name)

    return {
        "alert_name": alert_name,
        "alert_category": alert_category,
        "severity": severity,
        "status": status,
        "affected_resource": affected_resource,
        "resource_type": resource_type,
        "signal_type": signal_type,
        "monitoring_service": monitoring_service,
        "metric_name": metric_name,
        "metric_value": metric_value,
        "threshold": threshold,
        "operator": operator,
        "time_aggregation": time_aggregation
    }


# Format only the relevant normalized ocntext for the AI model
# This avoids sending the full raw alert payload and keeps the prompt focused on triage.

def format_context_for_ai(context: dict) -> str:
    return f"""
Alert Category: {context["alert_category"]}
Severity: {context["severity"]}
Status: {context["status"]}
Affected Resource: {context["affected_resource"]}
Resource Type: {context["resource_type"]}
Metric Name: {context["metric_name"]}
Metric Value: {context["metric_value"]}
Threshold: {context["threshold"]}
Operator: {context["operator"]}
Time Aggregation: {context["time_aggregation"]}
Monitoring Service: {context["monitoring_service"]}
"""


# Event-driven incident response workflow.
# Triggered by Azure Monitor Action Groups, enriches the alert with the AI analysis
# writes a structured incidnet reeport to log Analytics, and optionally send a logic App notification.  v c

@app.route(route="IncidentResponseWebhook", auth_level=func.AuthLevel.FUNCTION)
def IncidentResponseWebhook(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("IncidentResponseWebhook triggered")

    try:
        alert_payload = req.get_json()
        logging.info("Full alert payload received")
    except ValueError:
        return func.HttpResponse("Invalid JSON payload", status_code=400)

    dce_endpoint = os.environ["DCE_ENDPOINT"]
    incident_dcr_id = os.environ["INCIDENT_DCR_ID"]
    incident_stream = os.environ["INCIDENT_STREAM"]
    ollama_url = os.environ["OLLAMA_URL"]
    model = os.environ["OLLAMA_MODEL"]
    workspace_id = os.environ["WORKSPACE_ID"]

    credential = DefaultAzureCredential()
    token = credential.get_token("https://monitor.azure.com/.default").token

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    data = alert_payload.get("data", {})
    essentials = data.get("essentials", {})
    alert_context = data.get("alertContext", {})

    alert_name = essentials.get("alertRule", "Unknown Alert")
    severity = essentials.get("severity", "Unknown")
    status = essentials.get("monitorCondition", "Unknown")

    alert_target_ids = essentials.get("alertTargetIDs") or []
    configuration_items = essentials.get("configurationItems") or []

    affected_resource = (
        essentials.get("targetResourceName")
        or essentials.get("targetResource")
        or (alert_target_ids[0].split("/")[-1] if alert_target_ids else None)
        or (configuration_items[0] if configuration_items else None)
        or essentials.get("monitoringService")
        or "Unknown Resource"
    )

    resource_type = "Unknown Resource Type"

    if alert_target_ids and "/providers/" in alert_target_ids[0]:
        provider_path = alert_target_ids[0].split("/providers/")[-1]
        provider_parts = provider_path.split("/")

        if len(provider_parts) >= 2:
            resource_type = f"{provider_parts[0]}/{provider_parts[1]}"
        elif len(provider_parts) == 1:
            resource_type = provider_parts[0]

    incident_context = build_incident_context(
        essentials,
        alert_context,
        affected_resource,
        resource_type
    )

    ai_context = format_context_for_ai(incident_context)
    logging.info("Normalized incident context prepared for AI")

# Provide safe fallback incident text so the workflow still produces usable output
# if the AI model is unavailable, slow, or returns malformed content.

    base_incident_summary = (
        f"Incident Summary: Azure Monitor detected {incident_context['alert_category']} "
        f"on {affected_resource} with severity {severity} and status {status}."
    )

    base_recommended_action = (
        "Recommended Actions: Review the affected resource metrics, validate workload activity, "
        "and escalate if the condition is unexpected or service-impacting."
    )

    ai_incident_summary = base_incident_summary
    ai_recommended_action = base_recommended_action

# Ask the model for constrained JSON output so downstream parsing remains predictable.
# Low temperature is used to reduce variatio in incident triage responses.

    ai_prompt = f"""
You are a senior Azure cloud operations engineer performing production incident triage.

Use the alert context below to create a concise incident analysis.

Alert Context:
{ai_context}

Return ONLY valid JSON. Do not include markdown, explanations, headings, placeholders, or extra text.

The JSON must use exactly these keys:
{{
  "incident_summary": "1-2 complete sentences explaining what happened and why it matters.",
  "likely_root_cause": "1 complete sentence explaining the most likely technical cause.",
  "recommended_actions": "Exactly 3 short numbered actions an Azure support engineer should take.",
  "potential_impact": "1 complete sentence explaining the operational risk if unresolved."
}}

Rules:
Do not repeat the alert rule name.
Do not copy the raw alert context back into the response.
Do not mention the prompt or instructions.
Do not include placeholder text.
Keep the wording practical and operational.
"""

    try:
        ai_payload = {
            "model": model,
            "prompt": ai_prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.1,
                "num_predict": 420
            }
        }

        ai_response = requests.post(
            ollama_url,
            json=ai_payload,
            timeout=180
        )
        ai_response.raise_for_status()

        ai_text = ai_response.json().get("response", "").strip()

        if ai_text:
            logging.info("Ollama incident triage generated")
            logging.info("Raw Ollama incident triage response: %s", ai_text)

            ai_json = extract_json_from_ai_response(ai_text)

            incident_summary = clean_ai_text(ai_json.get("incident_summary", ""))
            likely_root_cause = clean_ai_text(ai_json.get("likely_root_cause", ""))
            recommended_actions = clean_ai_text(ai_json.get("recommended_actions", ""))
            potential_impact = clean_ai_text(ai_json.get("potential_impact", ""))

            summary_parts = []

            if incident_summary:
                summary_parts.append(f"Incident Summary: {incident_summary}")

            if likely_root_cause:
                summary_parts.append(f"Likely Root Cause: {likely_root_cause}")

            if potential_impact:
                summary_parts.append(f"Potential Impact: {potential_impact}")

            if summary_parts:
                ai_incident_summary = "\n\n".join(summary_parts)
            else:
                logging.warning("AI JSON did not contain usable summary fields. Keeping fallback summary.")

            if recommended_actions:
                ai_recommended_action = f"Recommended Actions: {recommended_actions}"
            else:
                logging.warning("AI JSON did not contain usable recommended_actions. Keeping fallback recommendation.")

        else:
            logging.warning("Ollama returned an empty triage response")

    except Exception as e:
        logging.error("Ollama incident triage failed: %s", str(e))

# Build the structured incident report payload for custom Log Analytics ingestion.

    incident_payload = [
        {
            "TimeGenerated": datetime.utcnow().isoformat() + "Z",
            "AlertName": alert_name,
            "Severity": severity,
            "AffectedResource": affected_resource,
            "IncidentSummary": ai_incident_summary,
            "RecommendedAction": ai_recommended_action,
            "Status": status
        }
    ]

    ingestion_url = (
        f"{dce_endpoint}/dataCollectionRules/{incident_dcr_id}"
        f"/streams/{incident_stream}?api-version=2023-01-01"
    )

    response = requests.post(
        ingestion_url,
        headers=headers,
        json=incident_payload,
        timeout=30
    )

    logging.info("Incident ingestion status: %s", response.status_code)
    logging.info("Incident ingestion response: %s", response.text)

    if response.status_code not in [200, 202, 204]:
        return func.HttpResponse(
            f"Failed to ingest incident report: {response.status_code} {response.text}",
            status_code=500
        )

# Send an external notification only when a Logic app webhook is configured
# and the dedupe check confirms this is not a recently processed duplicate.

    logic_app_url = os.environ.get("LOGIC_APP_WEBHOOK_URL")

    send_notification = should_send_notification(
    	workspace_id,
    	alert_name,
    	affected_resource,
    	status
    )

    if logic_app_url and send_notification:
        try:
            logic_app_response = requests.post(
                logic_app_url,
                json=incident_payload[0],
                timeout=20
            )

            logging.info("Logic App notification status: %s", logic_app_response.status_code)
            logging.info("Logic App notification response: %s", logic_app_response.text)

        except Exception as e:
            logging.error("Logic App notification failed: %s", str(e))

    elif logic_app_url and not send_notification:
        logging.info(
            "Duplicate alert notification suppressed for %s / %s / %s",
            alert_name,
            affected_resource,
            status
        )

    else:
        logging.warning("LOGIC_APP_WEBHOOK_URL app setting not configured. Skipping notification.")

    return func.HttpResponse(
        "Incident alert processed, AI-enriched, and written to Log Analytics.",
        status_code=200
    )