## The review requests changes

The review of `{{ build.journal.last_implementation.head_sha }}` requests changes:

{{ build.journal.requested_changes.summary }}

{% for finding in build.journal.requested_changes.findings %}
- **{{ finding.severity }}** {{ finding.summary }}{% if finding.path %} (`{{ finding.path }}{% if finding.line %}:{{ finding.line }}{% endif %}`){% endif %}: {{ finding.evidence }}
{% endfor %}

Address each finding that your diff left open and deliver it as before. Dismiss only the reviews and resolve only the threads that your new commits addressed, and republish the plan block in the pull request body for this revision.
