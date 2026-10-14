# © 2023 Amazon Web Services, Inc. or its affiliates. All Rights Reserved.
# This AWS Content is provided subject to the terms of the AWS Customer Agreement available at
# http: // aws.amazon.com/agreement or other written agreement between Customer and either
# Amazon Web Services, Inc. or Amazon Web Services EMEA SARL or both.
from slack_sdk import WebClient
import os
import json
import boto3
from datetime import datetime, timezone
from dateutil import parser, tz

session = boto3.Session()


def parse_arn(arn):
    # http://docs.aws.amazon.com/general/latest/gr/aws-arns-and-namespaces.html
    elements = arn.split(":")
    result = {
        "arn": elements[0],
        "partition": elements[1],
        "service": elements[2],
        "region": elements[3],
        "account": elements[4],
    }
    if len(elements) == 7:
        result["resourcetype"], result["resource"] = elements[5:]
    elif "/" not in elements[5]:
        result["resource"] = elements[5]
        result["resourcetype"] = None
    else:
        result["resourcetype"], result["resource"] = elements[5].split("/")
    return result


def send_ses_notification(
    source_email, source_arn, subject, message_html, to_addresses, cc_addresses
):
    try:
        # Providing a source arn enables using an SES identity in another account
        if source_arn:
            ses_region = parse_arn(source_arn)["region"]
            ses_client = session.client("ses", region_name=ses_region)

            ses_client.send_email(
                Source=source_email,
                SourceArn=source_arn,
                Destination={"ToAddresses": to_addresses, "CcAddresses": cc_addresses},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Html": {"Data": message_html, "Charset": "UTF-8"}},
                },
            )
        else:
            ses_client = session.client("ses")
            ses_client.send_email(
                Source=source_email,
                Destination={"ToAddresses": to_addresses, "CcAddresses": cc_addresses},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Html": {"Data": message_html, "Charset": "UTF-8"}},
                },
            )
    except Exception as e:
        print(f"Error sending email via SES: {e}")


def send_sns_notification(notification_topic_arn, message, subject):
    sns_client = session.client("sns")
    try:
        sns_client.publish(
            TopicArn=notification_topic_arn,
            Message=message,
            Subject=subject,
        )
    except Exception as e:
        print(f"Error publishing message to SNS: {e}")


SLACK_CHANNEL_TIMEZONE = tz.gettz("America/Sao_Paulo")


def slack_messages(status, requester, approver, error, mention, mention_all):
    """Returns (direct message, channel message) in pt-BR for a request status.

    The channel message is None for statuses that are not posted to the channel.
    """
    match status:
        case "pending":
            return (
                f"🔐 {mention(requester)} pediu acesso à AWS. Revise a solicitação e aprove ou recuse no TEAM.",
                f"🔐 *Nova solicitação de acesso AWS*\n{mention(requester)} pediu acesso — {mention_all()}, podem revisar?",
            )
        case "scheduled":
            return ("🗓️ Sua sessão de acesso AWS foi agendada.", None)
        case "expired":
            return ("⌛ Sua solicitação de acesso AWS expirou sem ser revisada.", None)
        case "ended":
            return ("⚪ Sua sessão de acesso AWS foi encerrada.", None)
        case "granted":
            return ("🟢 Sua sessão de acesso AWS começou.", None)
        case "approved":
            message = f"✅ Solicitação de acesso AWS de {mention(requester)} aprovada por {mention(approver)}."
            return (message, message)
        case "rejected":
            message = f"❌ Solicitação de acesso AWS de {mention(requester)} recusada por {mention(approver)}."
            return (message, message)
        case "cancelled":
            message = f"🚫 {mention(requester)} cancelou a solicitação de acesso AWS."
            return (message, message)
        case "error":
            return (
                f"⚠️ Erro ao processar o acesso AWS de {mention(requester)}. Detalhes: {error}",
                None,
            )


def format_slack_date(start_time, timezone_):
    localized_date = parser.parse(start_time).astimezone(timezone_)
    return localized_date.strftime("%d/%m/%Y às %H:%M")


def format_slack_duration(duration_hours):
    return f"{duration_hours} hora" if str(duration_hours) == "1" else f"{duration_hours} horas"


def slack_blocks(message, button, account, role, start_time, duration_hours, justification, ticket):
    fields = [
        {"type": "mrkdwn", "text": f"*Conta:*\n{account}"},
        {"type": "mrkdwn", "text": f"*Role:*\n{role}"},
        {"type": "mrkdwn", "text": f"*Início:*\n{start_time}"},
        {"type": "mrkdwn", "text": f"*Duração:*\n{format_slack_duration(duration_hours)}"},
        {"type": "mrkdwn", "text": f"*Justificativa:*\n{justification or '-'}"},
    ]
    if ticket:
        fields.append({"type": "mrkdwn", "text": f"*Ticket:*\n{ticket}"})

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": message}},
        {"type": "section", "fields": fields},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": button["text"]},
                    "url": button["url"],
                    "action_id": "button-action",
                    **({"style": "primary"} if button.get("primary") else {}),
                }
            ],
        },
    ]


def send_slack_notifications(
    status,
    recipients,
    requester,
    approver,
    approvers,
    error,
    team_url,
    request_start_time,
    role,
    account,
    duration_hours,
    justification,
    ticket,
):
    try:
        dynamodb = session.resource("dynamodb")
        settings_table_name = os.getenv("SETTINGS_TABLE_NAME")
        settings_table = dynamodb.Table(settings_table_name)
        settings = settings_table.get_item(Key={"id": "settings"})
        item_settings = settings.get("Item", {})
        slack_token = item_settings.get("slackToken", "")
        slack_audit_notifications_channel = item_settings.get(
            "slackAuditNotificationsChannel", ""
        )
        if slack_token:
            slack_client = WebClient(token=slack_token)
    except Exception as error:
        print(
            f"Error retrieving Slack OAuth token, cannot send Slack notifications: {error}"
        )
        return

    slack_users = {}

    def get_slack_user(email):
        if email not in slack_users:
            try:
                slack_users[email] = slack_client.users_lookupByEmail(email=email)["user"]
            except Exception as error:
                print(f"Error getting Slack user info for {email}: {error}")
                slack_users[email] = None
        return slack_users[email]

    def mention(email):
        slack_user = get_slack_user(email) if email else None
        return f"<@{slack_user['id']}>" if slack_user else (email or "-")

    def mention_all():
        return ", ".join(mention(email) for email in approvers or []) or "aprovadores"

    direct_message, channel_message = slack_messages(
        status, requester, approver, error, mention, mention_all
    )

    # Pending requests send approvers straight to the approvals page
    if status == "pending":
        button = {
            "text": "Revisar no TEAM",
            "url": f"{team_url}/approvals/approve",
            "primary": True,
        }
    else:
        button = {"text": "Abrir no TEAM", "url": team_url}

    for recipient in recipients:
        recipient_slack_user = get_slack_user(recipient)
        if not recipient_slack_user:
            continue

        # Format date, localized to recipient's timezone
        recipient_timezone = tz.gettz(name=recipient_slack_user["tz"])
        message_blocks = slack_blocks(
            direct_message,
            button,
            account,
            role,
            format_slack_date(request_start_time, recipient_timezone),
            duration_hours,
            justification,
            ticket,
        )

        # Send message to user
        try:
            slack_client.chat_postMessage(
                channel=recipient_slack_user["id"],
                blocks=message_blocks,
                text=direct_message,
            )
        except Exception as error:
            print(
                f"Error posting chat message to channel/user id {recipient_slack_user['id']}: {error}"
            )

    # send audit notifications to channel if defined
    if slack_audit_notifications_channel and channel_message:
        try:
            slack_client.chat_postMessage(
                channel=slack_audit_notifications_channel,
                text=channel_message,
                blocks=slack_blocks(
                    channel_message,
                    button,
                    account,
                    role,
                    format_slack_date(request_start_time, SLACK_CHANNEL_TIMEZONE),
                    duration_hours,
                    justification,
                    ticket,
                ),
            )
        except Exception as error:
            print(
                f"Error posting audit message to channel {slack_audit_notifications_channel} (ensure bot is invited to the channel): {error}"
            )


def lambda_handler(event: dict, context):
    ses_notifications_enabled = event.get("ses_notifications_enabled", "")
    ses_source_email = event.get("ses_source_email", "")
    ses_source_arn = event.get("ses_source_arn", "")
    sns_notifications_enabled = event.get("sns_notifications_enabled", "")
    notification_topic_arn = event.get("notification_topic_arn", "")
    slack_notifications_enabled = event.get("slack_notifications_enabled", "")
    if not (
        (ses_notifications_enabled and ses_source_email)
        or (sns_notifications_enabled and notification_topic_arn)
        or (slack_notifications_enabled)
    ):
        # Notifications are disabled or configuration is invalid
        return

    approval_required = event["approvalRequired"]
    request_status = event["status"]
    granted = (
        event.get("grant", {})
        .get("AccountAssignmentCreationStatus", {})
        .get("Status", "")
        == "IN_PROGRESS"
    )
    ended = (
        event.get("revoke", {})
        .get("AccountAssignmentDeletionStatus", {})
        .get("Status", "")
        == "IN_PROGRESS"
    )
    # These statuses are updated in the DDB table after the event is generated, so we update it here
    if ended:
        request_status = "ended"
    if (
        (request_status == "approved" or request_status == "pending")
        and granted
        and not ended
    ):
        request_status = "granted"
    if not granted and not approval_required:
        request_status = "scheduled"
    event.update(
        {
            "status": request_status,
        }
    )

    requester = event["email"]
    approvers = event.get("approvers", "")
    account = f'{event["accountName"]} ({event["accountId"]})'
    role = event["role"]
    request_start_time = event["startTime"]
    duration_hours = event["time"]
    justification = event.get("justification", "No justification provided")
    ticket = event.get("ticketNo", "No ticket provided")
    login_url = event["sso_login_url"]
    sns_message = json.dumps(event)
    team_url = os.getenv("TEAM_URL", "").rstrip("/") or login_url

    match request_status:
        case "pending":
            if approval_required:
                # Notify approvers pending request
                slack_recipients = approvers
                email_to_addresses = approvers
                email_cc_addresses = [requester]
                subject = f"Access request to AWS account - {account}"
                email_message_html = f'<html><body><p><b>{requester}</b> requests access to AWS, please <b>approve or reject this request</b> in <a href="{login_url}">TEAM</a>.</p><p><b>Account:</b> {account}<br /><b>Role:</b> {role}<br /><b>Start Time:</b> {request_start_time}<br /><b>Duration:</b> {duration_hours} hours<br /><b>Justification:</b> {justification}<br /><b>Ticket Number:</b> {ticket}<br /></p></body></html>'
        case "scheduled":
            # Don't need to send a notification if the request start time has already passed
            if datetime.now(timezone.utc) > parser.parse(request_start_time).astimezone(
                timezone.utc
            ):
                return
            # Notify requester request scheduled
            slack_recipients = [requester]
            email_to_addresses = [requester]
            email_cc_addresses = []
            subject = f"Scheduled access session for {account}"
            email_message_html = f'<html><body><p>Your AWS access session is scheduled, please open <a href="{login_url}">TEAM</a> to manage requests.</p><p><b>Account:</b> {account}<br /><b>Role:</b> {role}<br /><b>Start Time:</b> {request_start_time}<br /><b>Duration:</b> {duration_hours} hours<br /><b>Justification:</b> {justification}<br /><b>Ticket Number:</b> {ticket}<br /></p></body></html>'
        case "expired":
            # Notify requester request expired
            slack_recipients = [requester]
            email_to_addresses = [requester]
            email_cc_addresses = approvers if approval_required else []
            subject = f"Expired access request for {account}"
            email_message_html = f'<html><body><p>Your AWS access request has expired, please open <a href="{login_url}">TEAM</a> to submit a new request.</p><p><b>Account:</b> {account}<br /><b>Role:</b> {role}<br /><b>Start Time:</b> {request_start_time}<br /><b>Duration:</b> {duration_hours} hours<br /><b>Justification:</b> {justification}<br /><b>Ticket Number:</b> {ticket}<br /></p></body></html>'
        case "ended":
            # Notify requester ended
            slack_recipients = [requester]
            email_to_addresses = [requester]
            email_cc_addresses = approvers if approval_required else []
            subject = f"AWS access session ended for {account}"
            email_message_html = f'<html><body><p>Your AWS access session has ended, please open <a href="{login_url}">TEAM</a> to view session activity logs.</p><p><b>Account:</b> {account}<br /><b>Role:</b> {role}<br /><b>Start Time:</b> {request_start_time}<br /><b>Duration:</b> {duration_hours} hours<br /><b>Justification:</b> {justification}<br /><b>Ticket Number:</b> {ticket}<br /></p></body></html>'
        case "granted":
            # Notify requester access granted
            slack_recipients = [requester]
            email_to_addresses = [requester]
            email_cc_addresses = approvers if approval_required else []
            subject = f"AWS access session started for {account}"
            email_message_html = f'<html><body><p>Your AWS access session has started. Open <a href="{login_url}">TEAM</a> to manage AWS access requests.</p><p><b>Account:</b> {account}<br /><b>Role:</b> {role}<br /><b>Start Time:</b> {request_start_time}<br /><b>Duration:</b> {duration_hours} hours<br /><b>Justification:</b> {justification}<br /><b>Ticket Number:</b> {ticket}<br /></p></body></html>'
        case "approved":
            # Notify requester request approved
            slack_recipients = approvers + [requester]
            email_to_addresses = [requester]
            email_cc_addresses = approvers
            subject = f"AWS access request approved for {account}"
            email_message_html = f'<html><body><p>Your AWS access request has been approved by {event["approver"]}. You will receive a notification when the session has started. Open <a href="{login_url}">TEAM</a> to manage AWS access requests.</p><p><b>Account:</b> {account}<br /><b>Role:</b> {role}<br /><b>Start Time:</b> {request_start_time}<br /><b>Duration:</b> {duration_hours} hours<br /><b>Justification:</b> {justification}<br /><b>Ticket Number:</b> {ticket}<br /></p></body></html>'
        case "rejected":
            # Notify requester request rejected
            slack_recipients = approvers + [requester]
            email_to_addresses = [requester]
            email_cc_addresses = approvers
            subject = f"AWS access request rejected for {account}"
            email_message_html = f'<html><body><p>Your AWS access request has been rejected. Open <a href="{login_url}">TEAM</a> to manage AWS access requests.</p><p><b>Account:</b> {account}<br /><b>Role:</b> {role}<br /><b>Start Time:</b> {request_start_time}<br /><b>Duration:</b> {duration_hours} hours<br /><b>Justification:</b> {justification}<br /><b>Ticket Number:</b> {ticket}<br /></p></body></html>'
        case "cancelled":
            # Notify approvers request cancelled
            slack_recipients = approvers
            email_to_addresses = approvers
            email_cc_addresses = [requester]
            subject = f"AWS access request cancelled for {account}"
            email_message_html = f'<html><body><p>{requester} cancelled an AWS access request. Open <a href="{login_url}">TEAM</a> to manage AWS access requests.</p><p><b>Account:</b> {account}<br /><b>Role:</b> {role}<br /><b>Start Time:</b> {request_start_time}<br /><b>Duration:</b> {duration_hours} hours<br /><b>Justification:</b> {justification}<br /><b>Ticket Number:</b> {ticket}<br /></p></body></html>'
        case "error":
            # Notify approvers and requester error
            slack_recipients = approvers + [requester]
            email_to_addresses = [ses_source_email]
            email_cc_addresses = approvers + [requester]
            subject = f"Error handling AWS access for {account}"
            email_message_html = f'<html><body><p>TEAM encountered an error handling AWS access for {requester}. Please review the Step Function logs to troubleshoot the error and ensure access is properly granted or revoked. Open <a href="{login_url}">TEAM</a> to view additional details.</p><p><b>Error Details:</b> {event.get("statusError")}<br /></p><p><b>Account:</b> {account}<br /><b>Role:</b> {role}<br /><b>Start Time:</b> {request_start_time}<br /><b>Duration:</b> {duration_hours} hours<br /><b>Justification:</b> {justification}<br /><b>Ticket Number:</b> {ticket}<br /></p></body></html>'
        case _:
            print(f"Request status unexpected, exiting: {request_status}")
            return

    if ses_notifications_enabled:
        send_ses_notification(
            source_email=ses_source_email,
            source_arn=ses_source_arn,
            message_html=email_message_html,
            subject=subject,
            to_addresses=email_to_addresses,
            cc_addresses=email_cc_addresses,
        )

    if sns_notifications_enabled:
        send_sns_notification(
            message=sns_message,
            subject=subject,
            notification_topic_arn=notification_topic_arn,
        )

    if slack_notifications_enabled:
        send_slack_notifications(
            status=request_status,
            recipients=slack_recipients,
            requester=requester,
            approver=event.get("approver", ""),
            approvers=approvers,
            error=event.get("statusError"),
            team_url=team_url,
            role=role,
            account=account,
            request_start_time=request_start_time,
            duration_hours=duration_hours,
            justification=event.get("justification", ""),
            ticket=event.get("ticketNo", ""),
        )
