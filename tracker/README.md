This Django app contains all of the data regarding jobs as well as the means to collect them.

This may not be the most accurate and functions best as a historical record of job applications and postings.

There are notable models existing
- Company
- Posting (tied to Company) - ATS score, job description
- Location (San Francisco)

Disregard all csv files unless explicitly asked.

Tools
- generate_insights - generate an html file insights_template.html that shows metrics on the last 500 job postings with software engineering