"""Hard constraints use structured facts; omissions stay UNKNOWN."""
import re

DEGREES = {"高中": 0, "大专": 1, "本科": 2, "bachelor": 2,
           "硕士": 3, "master": 3, "博士": 4, "phd": 4}


def job_kind(job):
    if job.job_type:
        return job.job_type
    if "实习" in job.title or re.search(r"\bintern(?:ship)?\b", job.title, re.I):
        return "internship"
    return "campus_recruiting" if job.campus is True else ""


def role_priority(title, prefs):
    return 2 if any(x.casefold() in title.casefold() for x in prefs.secondary_job_titles) else 1


def evaluate(job, ctx):
    prefs = ctx.preferences
    rejected, unknown = [], []
    text = (job.title + " " + job.jd).casefold()
    if not job.active:
        rejected.append("岗位已失效")
    if any(x.casefold() in job.company.casefold() for x in prefs.company_blacklist):
        rejected.append("公司黑名单")
    if not any(x.casefold() in job.title.casefold() for x in prefs.job_titles):
        rejected.append("岗位类别不符")
    if any(x.casefold() in text for x in prefs.keywords_exclude):
        rejected.append("排除词")
    if any(x.casefold() not in text for x in prefs.keywords_must_have):
        rejected.append("必要关键词缺失")
    locations = prefs.locations
    allowed = locations.get("preferred", []) + locations.get("acceptable", [])
    if job.remote is True and prefs.allow_remote is False:
        rejected.append("不接受远程")
    if job.remote is True and prefs.allow_remote is True:
        # Remote work does not establish residency, timezone or work eligibility.
        unknown.append("remote_eligibility")
    elif not job.city:
        unknown.append("city")
    elif any(x.casefold() in job.city.casefold() for x in locations.get("excluded", [])):
        rejected.append("排除地点")
    elif allowed and not any(x.casefold() in job.city.casefold() for x in allowed):
        rejected.append("地点不符")
    degree = DEGREES.get(job.minimum_degree.casefold())
    degrees = [DEGREES.get(x.degree.casefold()) for x in ctx.profile.education]
    known_degrees = [x for x in degrees if x is not None]
    if degree is None or not known_degrees:
        unknown.append("degree")
    elif degree > max(known_degrees):
        rejected.append("学历门槛")
    kinds = prefs.job_types or [prefs.job_type]
    kind = job_kind(job)
    if kind and kind not in kinds:
        rejected.append("用工类型不符")
    elif not kind:
        unknown.append("job_type")
    internship = kind == "internship"
    period = "day" if internship else "month"
    unit = "daily" if internship else "monthly"
    low = prefs.compensation.get(f"minimum_{unit}_cny")
    high = prefs.compensation.get(f"maximum_{unit}_cny")
    comparable = (job.salary_currency.upper() == "CNY" and job.salary_period == period
                  and (bool(kind) or kinds == ["campus_recruiting"]))
    if not comparable or job.salary_min is None or job.salary_max is None:
        unknown.append("salary")
    if comparable:
        if low is not None and job.salary_max is not None and job.salary_max < float(low):
            rejected.append("薪资低于下限")
        if high is not None and job.salary_min is not None and job.salary_min > float(high):
            rejected.append("薪资超出偏好边界")
    if kind == "campus_recruiting" or (not kind and kinds == ["campus_recruiting"]):
        if job.campus is None:
            unknown.append("campus")
        elif not job.campus:
            rejected.append("非应届岗位")
        if job.graduation_year is None:
            unknown.append("graduation_year")
        elif prefs.expected_graduation_year and job.graduation_year != prefs.expected_graduation_year:
            rejected.append("毕业年份不符")
    if internship:
        if prefs.internship_start_month and (not job.start_month or job.start_month < prefs.internship_start_month):
            unknown.append("internship_start")
        if prefs.internship_max_days_per_week is not None:
            if job.days_per_week is None:
                unknown.append("internship_days")
            elif job.days_per_week > prefs.internship_max_days_per_week:
                rejected.append("每周出勤超过上限")
        if prefs.internship_min_months is not None and (job.duration_months is None or job.duration_months > prefs.internship_min_months):
            unknown.append("internship_duration")
    if job.experience_years is None:
        unknown.append("experience")
    elif prefs.max_experience_years is not None and job.experience_years > prefs.max_experience_years:
        rejected.append("经验门槛")
    return {"decision": "REJECT" if rejected else ("UNKNOWN" if unknown else "PASS"),
            "reasons": rejected, "unknown": unknown}
