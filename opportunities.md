---
layout: default
title: Opportunities
permalink: /opportunities/
---

{% assign data = site.data.opportunities %}
<section class="page-header">
  <p class="eyebrow">Updated {{ data.updated_at | default: "not yet" }}</p>
  <h1>{{ data.title | default: "Merlion Radar" }}</h1>
  <p>{{ data.search_focus }}</p>
</section>

{% if data.stats %}
<section class="stats-grid">
  <div><strong>{{ data.stats.published_count }}</strong><span>published</span></div>
  <div><strong>{{ data.stats.candidates_scored }}</strong><span>scored</span></div>
  <div><strong>{{ data.stats.alert_count }}</strong><span>alerts</span></div>
</section>
{% endif %}

<section class="cards">
{% for job in data.jobs %}
  <article class="card">
    <div class="card-topline">
      <span class="badge badge-{{ job.status_badge | slugify }}">{{ job.status_badge }}</span>
      <span class="score">{{ job.score }}/100</span>
    </div>
    <h2><a href="{{ job.url }}" rel="noopener noreferrer">{{ job.title }}</a></h2>
    <p class="meta">{{ job.company }} · {{ job.location }} · {{ job.source }}</p>
    <p>{{ job.summary }}</p>

    <div class="chips">
      {% for tag in job.tags %}<span>{{ tag }}</span>{% endfor %}
    </div>

    <section class="action-plan">
      <h3>Action plan</h3>
      <p><strong>Why match:</strong> {{ job.why_match }}</p>
      <p><strong>Next action:</strong> {{ job.next_action }}</p>
    </section>

    <section class="relevance-plan">
      <h3>Relevance plan</h3>
      <div>
        <h4>Skillsets to build</h4>
        <ul>{% for item in job.skillsets_to_build %}<li>{{ item }}</li>{% endfor %}</ul>
      </div>
      <div>
        <h4>Certs / courses to consider</h4>
        <ul>{% for item in job.certifications_to_consider %}<li>{{ item }}</li>{% endfor %}</ul>
      </div>
      <div>
        <h4>Learning gaps to close</h4>
        <ul>{% for item in job.learning_gaps %}<li>{{ item }}</li>{% endfor %}</ul>
      </div>
    </section>
  </article>
{% endfor %}
</section>

{% if data.jobs == empty %}
<section class="panel"><p>No opportunities generated yet. Run <code>python3 scripts/update_opportunities.py</code>.</p></section>
{% endif %}
