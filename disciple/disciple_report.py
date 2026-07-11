"""CLI client that reports disciple study activity to the tactical server.

Counts verse groups whose processed_at falls on the report day (yesterday
by default, in local time) and submits the result to the "Spiritual
reflection" question of the "Presence and Reflection" survey:
0 processed -> no credit, 1 -> partial credit, 2+ -> full credit.

Stateless: re-running for the same day recomputes and resubmits the same
result, so manual runs cannot mis-credit or consume anything.
"""

import sys
import sqlite3
import asyncio
import argparse
import datetime

from grpc import aio
from aapis.tactical.v1 import tactical_pb2_grpc, tactical_pb2

SURVEY_NAME = "Presence and Reflection"
QUESTION_NAME = "Spiritual reflection"


def _local_day_utc_bounds(day):
    """Return the UTC isoformat bounds [start, end) of a local calendar day,
    matching the naive-UTC isoformat strings stored in processed_at."""
    start_local = datetime.datetime.combine(day, datetime.time.min).astimezone()
    end_local = start_local + datetime.timedelta(days=1)
    to_utc = lambda dt: dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return to_utc(start_local).isoformat(), to_utc(end_local).isoformat()


def _count_processed_on(db_path, day):
    start, end = _local_day_utc_bounds(day)
    db = sqlite3.connect(db_path)
    try:
        return db.execute(
            "SELECT COUNT(*) FROM group_processed"
            " WHERE processed_at >= ? AND processed_at < ?",
            (start, end),
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
    parser.add_argument("--tactical-port", type=int, default=60060)
    parser.add_argument(
        "--report-date",
        type=str,
        default=None,
        help="Local date (YYYY-MM-DD) to report for; defaults to yesterday",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.report_date is not None:
        report_date = datetime.date.fromisoformat(args.report_date)
    else:
        report_date = datetime.date.today() - datetime.timedelta(days=1)

    count = _count_processed_on(args.db_path, report_date)
    result_type = _result_type(count)
    credit = tactical_pb2.SurveyQuestionResultType.Name(result_type)
    print(
        f"{count} group(s) processed on {report_date.isoformat()}; "
        f"reporting {credit}"
    )
    if args.dry_run:
        print("Dry run; not submitting.")
        return

    try:
        success = asyncio.run(_submit(args.tactical_port, report_date, result_type))
    except Exception as e:
        print(f"Failed to reach tactical server on port {args.tactical_port}: {e}")
        sys.exit(1)
    if not success:
        print("Tactical server rejected the survey result")
        sys.exit(1)
