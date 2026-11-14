"""Read, normalize and persist jobs without creating applications or calling models."""
from pydantic import ValidationError

from ..config import UserError
from .models import Job, SearchCriteria


def matches(job: Job, criteria: SearchCriteria):
    if not job.active:
        return False
    text = ' '.join((job.title, job.company, job.jd, job.category, *job.required_skills)).casefold()
    technical = ' '.join((job.title, job.jd, job.category, *job.required_skills)).casefold()
    return all(not terms or any(term.casefold() in value for term in terms) for terms, value in (
        (criteria.keywords, text), (criteria.cities, job.city.casefold()),
        (criteria.technical_directions, technical)))


def summary(row):
    keys = ('source', 'external_id', 'company', 'title', 'city', 'salary_text', 'salary_min',
            'salary_max', 'salary_currency', 'salary_period', 'minimum_degree', 'experience_years',
            'category', 'employment_type', 'required_skills', 'url', 'fetched_at', 'is_demo')
    return {'job_id': row['job_id'], **{key: row['job'][key] for key in keys}}


def discover(store, source, criteria: SearchCriteria):
    rows = source.search_jobs(criteria)
    normalized, errors = [], []
    # Validate the entire namespace before any job writes; mixed demo/live aborts.
    for index, raw in enumerate(rows, 1):
        try:
            job = source.normalize_job(raw)
        except UserError as error:
            if str(error) == 'V2_MODE_MISMATCH':
                raise
            errors.append({'row': index, 'error_code': 'V2_INVALID_JOB'})
            continue
        except (ValidationError, ValueError, TypeError, KeyError):
            errors.append({'row': index, 'error_code': 'V2_INVALID_JOB'})
            continue
        if job.is_demo != store.demo:
            raise UserError('V2_MODE_MISMATCH')
        normalized.append((index, job))
    invalid_count = len(errors)
    matched_count, filtered_out, storage_errors = 0, 0, 0
    selected, new_count, updated_count, duplicates = {}, 0, 0, 0
    for index, job in normalized:
        try:
            # Apply filters to what will actually be returned, including existing primaries.
            if not matches(store.preview_job(job), criteria):
                filtered_out += 1
                continue
            matched_count += 1
            if len(selected) >= criteria.limit:
                continue
            identifier, new, changed = store.save_job(job, source.policy)
        except UserError as error:
            if str(error) != 'V2_JOB_IDENTITY_CONFLICT':
                raise
            storage_errors += 1
            errors.append({'row': index, 'error_code': 'V2_JOB_IDENTITY_CONFLICT'})
            continue
        new_count += int(new)
        updated_count += int(changed)
        duplicates += int(identifier in selected)
        selected[identifier] = summary(store.get_job(identifier))
    partial = bool(errors or source.report.get('error_code'))
    return {'status': 'PARTIAL' if partial else 'COMPLETE', 'mode': 'DEMO' if store.demo else 'LIVE',
            'scope': 'P1_DISCOVERY_ONLY', 'criteria': criteria.model_dump(), 'acquisition': source.report,
            'source_policy': source.policy.model_dump(mode='json'), 'scanned': len(rows),
            'valid_count': len(normalized), 'invalid_count': invalid_count, 'storage_error_count': storage_errors,
            'filtered_out': filtered_out, 'match_count': matched_count,
            'selected_count': len(selected), 'new_count': new_count, 'updated_count': updated_count,
            'duplicate_rows_count': duplicates, 'errors': errors, 'jobs': list(selected.values()),
            'external_submission': False, 'model_called': False}
