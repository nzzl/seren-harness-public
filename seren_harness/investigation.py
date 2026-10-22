"""Data-only justification for a bounded customer investigation; no service access."""
import json

FIELDS = ('customer_job', 'consequence', 'hypothesis', 'evidence', 'new_information',
          'alternative', 'decision_if_pass', 'decision_if_fail')
REPEAT_BASES = ('new_hypothesis', 'instability_signal', 'relevant_change', 'bounded_measurement')


def proposal(text):
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as error:
        raise ValueError('rationale must be a JSON investigation proposal') from error
    if not isinstance(value, dict) or set(value) != set(FIELDS) | {'basis', 'repeat_reason'}:
        raise ValueError('proposal needs customer job, consequence, hypothesis, evidence, new information, '
                         'alternative, pass/fail decisions, basis and repeat_reason')
    if any(not isinstance(value[k], str) or not 20 <= len(value[k].strip()) <= 1200 for k in FIELDS):
        raise ValueError('each proposal field must be 20-1200 characters of concrete reasoning')
    if value['basis'] not in REPEAT_BASES or not isinstance(value['repeat_reason'], str):
        raise ValueError('invalid investigation/repeat basis')
    if value['basis'] != 'new_hypothesis' and not 40 <= len(value['repeat_reason'].strip()) <= 1200:
        raise ValueError('repeat requires a specific instability signal, relevant change or bounded measurement decision')
    if len(value['repeat_reason']) > 1200:
        raise ValueError('repeat reason too long')
    return value
