import logging
import os
from datetime import datetime
from functools import reduce
from typing import Any, Dict, List, NoReturn

import boto3
import requests
from botocore.exceptions import ClientError

SLACK_CHANNEL = os.environ['SLACK_CHANNEL']
SLACK_WEBHOOK_URL = os.environ['SLACK_WEBHOOK_URL']
TARGET_CURRENCY = os.environ['TARGET_CURRENCY']

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

ce = boto3.client('ce', region_name='us-east-1')


def convert_currency(source_price: float, source_currency: str, destination_currency: str) -> float:
    rate = get_rate(source_currency, destination_currency)

    return round(rate * source_price, 2)


def get_rate(base: str, to: str) -> float:
    response = requests.get('https://api.exchangeratesapi.io/latest?base={}'.format(base))

    if response.status_code == 200:
        response_body = response.json()
        rate = response_body['rates'][to]

        return rate


def get_cost_and_usage(start_date: str, end_date: str) -> List[Any]:
    results = []
    next_page_token = None
    try:

        while True:
            if next_page_token:
                kwargs = {'NextPageToken': next_page_token}
            else:
                kwargs = {}

            query_result = ce.get_cost_and_usage(
                TimePeriod={
                    'Start': start_date,
                    'End': end_date
                },
                Granularity='MONTHLY',
                Metrics=['BlendedCost'],
                GroupBy=[
                    {
                        'Type': 'DIMENSION',
                        'Key': 'SERVICE'
                    },
                ],
                **kwargs
            )

            results_by_time = query_result['ResultsByTime']
            groups = reduce(lambda a, b: a + b, [result.get('Groups') for result in results_by_time])
            for group in groups:
                logger.info(group)
                results.append(group)
            next_page_token = query_result.get('NextPageToken')
            if next_page_token is None:
                break

        logger.debug('Success to get AWS cost and usage')

        return results
    except ClientError as e:
        logger.warning(e)

        raise Exception('Encountered AWS exception')


def generate_slack_fields(service_costs: Dict[str, float]):
    fields = [{'title': key, 'value': value, 'short': 'true'} for key, value in service_costs.items() if value != 0]
    fields.append({'title': 'Total', 'value': sum(service_costs.values())})

    return fields


def post_to_slack(result) -> NoReturn:
    costs = {group['Keys'][0]: convert_currency(float(group['Metrics']['BlendedCost']['Amount']),
                                                group['Metrics']['BlendedCost']['Unit'], TARGET_CURRENCY) for group in
             result}
    fields = generate_slack_fields(costs)

    attachment = {
        'pretext': 'Monthly AWS cost report,',
        'color': 'good',
        'fields': fields,
    }
    slack_message = {
        'channel': SLACK_CHANNEL,
        'username': 'AWS',
        'icon_emoji': ':dollar:',
        'attachments': [attachment]
    }

    try:
        response = requests.post(SLACK_WEBHOOK_URL, json=slack_message)
        if response.status_code == 200:
            logger.info('Complete sending billing report to Slack')
        else:
            logger.info('Failed to send Slack Error {"status_code": ${}, "error": ${}}'.format(response.status_code,
                                                                                               response.text))
    except requests.exceptions.HTTPError as e:
        logger.error('connect - HTTP error: {}'.format(e))

        return None
    except requests.exceptions.RequestException as e:
        logger.error('connect - Request error: {}'.format(e))

        return None


def lambda_handler(event, context) -> NoReturn:
    logger.info('Initiate AWS billing reporter to slack')

    now = datetime.now()
    start_date_str = datetime(now.year, now.month, 1).strftime('%Y-%m-%d')
    current_date_str = now.strftime('%Y-%m-%d')

    results = get_cost_and_usage(start_date_str, current_date_str)
    post_to_slack(results)
