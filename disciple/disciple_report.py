"""CLI client that reports disciple study activity to the tactical server.

Counts verse groups processed since the last invocation and submits the
result to the "Spiritual reflection" question of the "Presence and
Reflection" survey: 0 processed -> no credit, 1 -> partial credit,
2+ -> full credit.
"""

import os
import sys
import json
import sqlite3
import asyncio
import argparse
import datetime

from grpc import aio
from aapis.tactical.v1 import tactical_pb2_grpc, tactical_pb2

SURVEY_NAME = "Presence and Reflection"
QUESTION_NAME = "Spiritual reflection"


def _load_last_checked(state_path, now):
    if os.path.exists(state_path):
        with open(state_path, "r") as f:
            return json.load(f)["last_checked"]
    return (now - datetime.timedelta(days=1)).isoformat()


def _save_last_checked(state_path, checked_at):
    with open(state_path, "w") as f:
        json.dump({"last_checked": checked_at}, f)


def _count_processed_since(db_path, since_iso):
    db = sqlite3.connect(db_path)
    try:
        return db.execute(
            "SELECT COUNT(*) FROM group_processed WHERE processed_at > ?",
            (since_iso,),
        ).fetchone()[0]
    finally:
        db.close()


def _result_type(count):
    if count >= 2:
        return tactical_pb2.SurveyQuestionResultType.SURVEY_QUESTION_RESULT_TYPE_FULL_CREDIT
    if count == 1:
        return tactical_pb2.SurveyQuestionResultType.SURVEY_QUESTION_RESULT_TYPE_PARTIAL_CREDIT
    return tactical_pb2.SurveyQuestionResultType.SURVEY_QUESTION_RESULT_TYPE_NO_CREDIT


async def _submit(port, date, result_type):
    async with aio.insecure_channel(f"localhost:{port}") as channel:
        stub = tactical_pb2_grpc.TacticalServiceStub(channel)
        response = await stub.SubmitSurveyResult(
            tactical_pb2.SubmitSurveyResultRequest(
                result=tactical_pb2.SurveyResult(
                    year=date.year,
                    month=date.month,
                    day=date.day,
                    survey_name=SURVEY_NAME,
                    results=[
                        tactical_pb2.SurveyQuestionResult(
                            question_name=QUESTION_NAME,
                            result=result_type,
                        )
                    ],
                )
            )
        )
    return response.success


def main():
    parser = argparse.ArgumentParser(
        description="Report disciple study activity to the tactical server"
    )
    parser.add_argument("--db-path", type=str, required=True)
    parser.add_argument(
        "--state-path", type=str, default="~/data/disciple/report-state.json"
    )
    parser.add_argument("--tactical-port", type=int, default=60060)
    parser.add_argument(
        "--report-date",
        type=str,
        default=None,
        help="Date (YYYY-MM-DD) to report for; defaults to yesterday",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    state_path = os.path.expanduser(args.state_path)
    now = datetime.datetime.utcnow()
    last_checked = _load_last_checked(state_path, now)
    count = _count_processed_since(args.db_path, last_checked)
    result_type = _result_type(count)
    if args.report_date is not None:
        report_date = datetime.date.fromisoformat(args.report_date)
    else:
        report_date = datetime.date.today() - datetime.timedelta(days=1)

    credit = tactical_pb2.SurveyQuestionResultType.Name(result_type)
    print(
        f"{count} group(s) processed since {last_checked}; "
        f"reporting {credit} for {report_date.isoformat()}"
    )
    if args.dry_run:
        print("Dry run; not submitting or updating state.")
        return

    try:
        success = asyncio.run(_submit(args.tactical_port, report_date, result_type))
    except Exception as e:
        print(f"Failed to reach tactical server on port {args.tactical_port}: {e}")
        sys.exit(1)
    if not success:
        print("Tactical server rejected the survey result")
        sys.exit(1)
    _save_last_checked(state_path, now.isoformat())
