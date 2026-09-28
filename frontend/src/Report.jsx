import { useState } from 'react'

const stripAnsi = (t) => (t || '').replace(/\x1b\[[0-9;]*[A-Za-z]/g, '')
const shortRepo = (u) => (u.startsWith('upload:') || u.startsWith('image:') ? u
  : u.replace(/\/+$/, '').replace(/\.git$/, '').split('/').slice(-2).join('/'))
const cls = (v) => (v == null ? 'na' : v >= 80 ? 'good' : v >= 50 ? 'warn' : 'bad')

const SCORE_ITEMS = [
  ['engineering_maturity', 'Engineering Maturity'], ['feature_completion', 'Feature Completion'],
  ['code_quality', 'Code Quality'], ['architecture', 'Architecture'], ['security', 'Security'],
  ['api_quality', 'API Quality'], ['deployment_readiness', 'Deployment Readiness'],
  ['structure', 'Project Structure'], ['database_connectivity', 'Database Connectivity'],
  ['authentication_flow', 'Authentication Flow'], ['error_handling', 'Error Handling'],
  ['security_configuration', 'Security Configuration'], ['required_features', 'Required Features'],
  ['environment_configuration', 'Environment Config'], ['documentation', 'Documentation'],
]

function Section({ icon, title, tone = 'info', badge, children }) {
  return (
    <section className={`sec tone-${tone}`}>
      <div className="sec-head">
        <span className="sec-ico">{icon}</span>
        <h3>{title}</h3>
        {badge}
      </div>
      <div className="sec-body">{children}</div>
    </section>
  )
}

const Bullets = ({ items }) => (
  <ul className="list">{items.map((s, i) => <li key={i}><span className="dot" /><span>{s}</span></li>)}</ul>
)

export function ScoreHero({ scores }) {
  const m = scores?.engineering_maturity
  if (!m) return null
  const v = m.score ?? 0
  const tone = m.partial ? 'na' : cls(m.score)
  const R = 44, C = 2 * Math.PI * R
  const verdict = m.partial ? 'Health check only - not comparable with full evaluations'
    : v >= 80 ? 'Excellent - close to production ready' : v >= 60 ? 'Good - a few gaps to close'
    : v >= 40 ? 'Needs work - several weak areas' : 'Critical gaps - major rework needed'
  const chips = []
  if (scores.build_seconds != null) chips.push(['Build time', `${scores.build_seconds}s`])
  const load = scores.load_test
  if (load) chips.push(['Avg latency', `${load.avg_latency_ms}ms`], ['p95', `${load.p95_latency_ms}ms`], ['Success', `${load.success_rate_percent}%`])
  const res = scores.resource_usage
  if (res?.captured && res.memory_usage_mb != null) chips.push(['Memory', `${res.memory_usage_mb}MB`])
  return (
    <div className={`hero tone-${tone}`}>
      <div className="ring">
        <svg viewBox="0 0 100 100">
          <defs>
            <linearGradient id="rg" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="var(--g1)" /><stop offset="100%" stopColor="var(--g2)" />
            </linearGradient>
          </defs>
          <circle cx="50" cy="50" r={R} className="ring-bg" />
          <circle cx="50" cy="50" r={R} className="ring-fg" stroke="url(#rg)" strokeDasharray={C} strokeDashoffset={C * (1 - v / 100)} />
        </svg>
        <div className="ring-num">{m.score ?? 'N/A'}<small>/100</small></div>
      </div>
      <div className="hero-body">
        <div className="hero-title">Engineering Maturity</div>
        <div className="hero-sub">{verdict}</div>
        <div className="chips">{chips.map(([k, val]) => <span className="chip" key={k}><b>{val}</b> {k}</span>)}</div>
      </div>
    </div>
  )
}

export function ScoreGrid({ scores }) {
  if (!scores) return null
  return (
    <div className="score-grid score-overview">
      {SCORE_ITEMS.map(([key, label]) => {
        if (!(key in scores)) return null
        const e = scores[key]
        const value = e ? e.score : null
        const partial = key === 'engineering_maturity' && e?.partial
        const rt = e?.runtime_evidence?.length ? ` (${e.runtime_evidence.join('; ')})` : ''
        return (
          <div className={`score-box ${partial ? 'na' : cls(value)}`} key={key}
            title={partial ? `Based on only ${e.components_averaged} check(s)` : `${e?.note || ''}${rt}`}>
            <div className="score-num">{value ?? 'N/A'}</div>
            <div className="score-label">{label}{partial ? ' (partial)' : ''}</div>
            <div className="score-bar"><span style={{ width: `${value ?? 0}%` }} /></div>
          </div>
        )
      })}
    </div>
  )
}

function Kpi({ label, value, sub, tone = 'info', children }) {
  return (
    <div className={`kpi tone-${tone}`}>
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
      {children}
    </div>
  )
}

export function DynamicTesting({ scores }) {
  if (!scores) return null
  const { testing, ui_smoke_check: smoke, load_test: load, resource_usage: res, api_health: health } = scores
  const cats = testing?.categories
  if (!testing && !smoke && !load && !health) return null
  const t = testing
  return (
    <Section icon="🧪" title="Dynamic Testing" tone="info">
      <div className="kpis">
        {t && (
          <Kpi label="Test suite"
            tone={t.ran ? (t.passed ? 'good' : t.timed_out ? 'warn' : 'bad') : 'na'}
            value={t.ran ? (t.passed ? 'Passed' : t.timed_out ? 'Timed out' : 'Failed') : 'Not run'}
            sub={!t.ran ? (t.reason || 'no tests') : t.network_used ? 'network used to install pytest' : null}>
            {t.ran && !t.passed && !t.timed_out && t.output && (
              <details><summary className="small">Test output</summary><pre className="report">{stripAnsi(t.output)}</pre></details>
            )}
          </Kpi>
        )}
        {health?.checked && (
          <Kpi label="API health" tone={health.reachable ? 'good' : 'bad'}
            value={health.reachable ? 'Reachable' : 'Unreachable'}
            sub={health.reachable ? `${health.endpoint}${health.status_code ? ` · HTTP ${health.status_code}` : ''}` : (health.reason || 'no response')}>
            {!health.reachable && (health.container_logs_tail || health.port_discovery_error) && (
              <details>
                <summary className="small">What the app printed</summary>
                {health.port_discovery_error && <div className="small">Port discovery: {health.port_discovery_error}</div>}
                {health.container_logs_tail && <pre className="report">{stripAnsi(health.container_logs_tail)}</pre>}
              </details>
            )}
          </Kpi>
        )}
        {smoke && (
          <Kpi label="Root URL check" tone={smoke.passed ? 'good' : 'bad'}
            value={smoke.passed ? 'Passed' : 'Failed'}
            sub={`HTTP ${smoke.status_code ?? 'n/a'}${smoke.passed && smoke.returns_html === false ? ' · API-only app' : ''}`} />
        )}
        {load && (
          <Kpi label="Load check" tone={cls(load.success_rate_percent)} value={`${load.success_rate_percent}% ok`}
            sub={`${load.requests_sent} req · avg ${load.avg_latency_ms}ms · p95 ${load.p95_latency_ms}ms`} />
        )}
        {scores.build_seconds != null && <Kpi label="Image build" tone="info" value={`${scores.build_seconds}s`} sub="Docker build time" />}
        {res?.captured && (
          <Kpi label="Resources" tone={res.memory_percent > 80 ? 'warn' : 'good'}
            value={res.memory_usage_mb != null ? `${res.memory_usage_mb}MB` : 'n/a'}
            sub={`${res.memory_limit_mb ?? '-'}MB limit · CPU ${res.cpu_percent != null ? `${res.cpu_percent}%` : 'n/a'}`}>
            {res.memory_percent != null && <div className="score-bar"><span style={{ width: `${res.memory_percent}%` }} /></div>}
          </Kpi>
        )}
      </div>
      {cats && (
        <div className="chips" style={{ marginTop: 14 }}>
          {[['Unit', 'unit_tests'], ['Integration', 'integration_tests'], ['UI', 'ui_smoke_tests'], ['Database', 'database_tests']].map(([l, k]) => (
            <span key={k} className={`chip ${cats[k] ? 'chip-good' : 'chip-bad'}`}>{cats[k] ? '✓' : '✗'} {l} tests</span>
          ))}
        </div>
      )}
    </Section>
  )
}

export function RuntimeAuthDbEvidence({ scores }) {
  if (!scores) return null
  const lines = [...(scores.authentication_flow?.runtime_evidence || []), ...(scores.database_connectivity?.runtime_evidence || [])]
  if (lines.length === 0) return null
  return (
    <Section icon="🔐" title="Runtime Auth / DB Evidence" tone="cyan" badge={<span className="pill">heavy probe · additive only</span>}>
      <p className="note">Conventional register / login / protected-route paths were probed on the running app. This can only raise Authentication Flow / Database Connectivity above the static score, never lower it.</p>
      <Bullets items={lines} />
    </Section>
  )
}

export function PlagiarismCheck({ scores }) {
  const c = scores?.plagiarism_check
  if (!c?.checked || !c.matches?.length) return null
  return (
    <Section icon="🔍" title="Plagiarism Check" tone={c.flagged ? 'bad' : 'warn'}
      badge={<span className={`pill ${c.flagged ? 'bad' : 'warn'}`}>{c.flagged ? 'flagged' : 'note'} · informational only</span>}>
      <p className="note">
        Compared against {c.compared_against} earlier submission(s) of the same project type. Structural similarity ignores names, so renamed copies are caught too.
        This is a heuristic, not proof of academic dishonesty - shared starter code can trigger it.
      </p>
      {c.boilerplate_ignored && c.boilerplate_ignored_percent > 0 && (
        <p className="note">{c.boilerplate_ignored_percent}% of this project's structure appears in many earlier submissions (scaffold code) and was ignored.</p>
      )}
      {c.matches.map((m, i) => (
        <div className="match" key={i}>
          <div className="match-top">
            <b>{shortRepo(m.repo_url)}</b>
            <span className={`pill ${m.flagged ? 'bad' : 'warn'}`}>{m.similarity_percent}%</span>
          </div>
          <div className="small">submission {m.submission_id} · exact {m.exact_percent ?? m.similarity_percent}%{m.structural_percent != null ? ` · structural ${m.structural_percent}%` : ''}</div>
          <div className={`score-bar tone-${m.flagged ? 'bad' : 'warn'}`}><span style={{ width: `${m.similarity_percent}%` }} /></div>
          {m.matched_files?.length > 0 && (
            <ul className="list">
              {m.matched_files.map((f, j) => (
                <li key={j}><span className="dot" /><span><code>{f.file}</code> is {f.match_percent}% contained in <code>{f.matched_file}</code></span></li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </Section>
  )
}

const FEEDBACK = [
  ['strengths', 'Strengths', '💪', 'good'], ['weaknesses', 'Weaknesses', '⚠️', 'warn'],
  ['missing_requirements', 'Missing Requirements', '📋', 'bad'], ['security_risks', 'Security Risks', '🛡️', 'bad'],
  ['performance_suggestions', 'Performance Suggestions', '⚡', 'info'], ['refactoring_suggestions', 'Refactoring Suggestions', '🔧', 'purple'],
]

export function Feedback({ feedback }) {
  if (!feedback) return null
  const road = feedback.improvement_roadmap || []
  return (
    <>
      <div className="sec-grid">
        {FEEDBACK.map(([key, label, icon, tone]) => {
          const items = feedback[key] || []
          if (!items.length) return null
          return (
            <Section key={key} icon={icon} title={label} tone={tone}
              badge={<span className="count">{items.length}</span>}>
              <Bullets items={items} />
            </Section>
          )
        })}
      </div>
      {road.length > 0 && (
        <Section icon="🗺️" title="Improvement Roadmap" tone="cyan" badge={<span className="count">{road.length} steps</span>}>
          <ol className="list steps">
            {road.map((s, i) => <li key={i}><span className="step-n">{i + 1}</span><span>{s}</span></li>)}
          </ol>
        </Section>
      )}
    </>
  )
}

export function AiReview({ review }) {
  if (!review) return null
  const blocks = [['strengths', 'Strengths', 'good'], ['concerns', 'Concerns', 'warn'], ['next_steps', 'Suggested next steps', 'info']]
  return (
    <Section icon="🤖" title="AI Review" tone="purple" badge={<span className="pill">AI-generated · advisory</span>}>
      <p className="note">
        Written by {review.model} from the results above. It never changes a score and can differ between runs.{' '}
        {review.code_snippets_sent ? 'A few redacted code excerpts were included.' : 'No source code was sent.'}
      </p>
      {review.summary && <p className="ai-summary">{review.summary}</p>}
      <div className="ai-cols">
        {blocks.map(([key, label, tone]) => {
          const items = review[key] || []
          if (!items.length) return null
          return (
            <div key={key} className={`ai-col tone-${tone}`}>
              <div className="ai-col-title">{label}</div>
              <Bullets items={items} />
            </div>
          )
        })}
      </div>
    </Section>
  )
}

// ---------- Markdown report viewer (dependency-free, no innerHTML) ----------
const ICONS = [['score', '📊'], ['strength', '💪'], ['weak', '⚠️'], ['missing', '📋'], ['security', '🛡️'], ['performance', '⚡'],
  ['refactor', '🔧'], ['roadmap', '🗺️'], ['improve', '🗺️'], ['test', '🧪'], ['plagiar', '🔍'], ['log', '🧾'], ['auth', '🔐'], ['summary', '📝']]
const iconFor = (t) => (ICONS.find(([k]) => t.toLowerCase().includes(k)) || [0, '📄'])[1]

function Inline({ text }) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)/g).filter(Boolean)
  return parts.map((p, i) => {
    if (p.startsWith('**') && p.endsWith('**') && p.length > 4) return <strong key={i}>{p.slice(2, -2)}</strong>
    if (p.startsWith('`') && p.endsWith('`') && p.length > 2) return <code key={i}>{p.slice(1, -1)}</code>
    if (p.startsWith('*') && p.endsWith('*') && p.length > 2) return <em key={i}>{p.slice(1, -1)}</em>
    return <span key={i}>{p}</span>
  })
}

function Cell({ text }) {
  const m = text.trim().match(/^(\d{1,3})\s*\/\s*100$/)
  if (m) {
    const n = Number(m[1])
    return (
      <div className={`md-score score-box ${cls(n)}`}>
        <b>{n}<small>/100</small></b>
        <div className="score-bar"><span style={{ width: `${n}%` }} /></div>
      </div>
    )
  }
  const t = text.trim().toLowerCase()
  if (['success', 'passed', 'true', 'yes'].includes(t)) return <span className="pill good">{text.trim()}</span>
  if (['failed', 'false', 'no', 'build_error'].includes(t)) return <span className="pill bad">{text.trim()}</span>
  return <Inline text={text} />
}

const splitRow = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim())
const META = /^\*\*(.+?):\*\*\s*(.*)$/

export function MarkdownView({ text }) {
  const lines = (text || '').replace(/\r/g, '').split('\n')
  const out = []
  let i = 0
  let k = 0

  while (i < lines.length) {
    const line = lines[i]

    if (!line.trim()) {
      i++
      continue
    }

    if (line.startsWith('```')) {
      const buf = []
      i++

      while (i < lines.length && !lines[i].startsWith('```')) {
        buf.push(lines[i++])
      }

      i++
      out.push(
        <pre className="report" key={k++}>
          {stripAnsi(buf.join('\n'))}
        </pre>
      )
      continue
    }

    const h = line.match(/^(#{1,4})\s+(.*)$/)

    if (h) {
      const level = h[1].length
      const title = h[2]

      out.push(
        level === 1
          ? <h2 className="md-title" key={k++}><Inline text={title} /></h2>
          : <h3 className={`md-h md-h${level}`} key={k++}>
              <span className="md-ico">{iconFor(title)}</span>
              <Inline text={title} />
            </h3>
      )

      i++
      continue
    }

    if (/^-{3,}$/.test(line.trim())) {
      out.push(<hr className="md-hr" key={k++} />)
      i++
      continue
    }

    if (line.startsWith('>')) {
      const buf = []

      while (i < lines.length && lines[i].startsWith('>')) {
        buf.push(lines[i++].replace(/^>\s?/, ''))
      }

      out.push(
        <div className="banner warn md-note" key={k++}>
          <Inline text={buf.join(' ')} />
        </div>
      )

      continue
    }

    /*
     * Markdown tables
     */
    if (
      line.trim().startsWith('|') &&
      i + 1 < lines.length &&
      /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])
    ) {
      const head = splitRow(line)
      i += 2

      const rows = []

      while (i < lines.length && lines[i].trim().startsWith('|')) {
        rows.push(splitRow(lines[i++]))
      }

      /* Score breakdown: preserve every category from the report table. */
      const scoreTable =
        (head[0] || '').trim().toLowerCase() === 'category' &&
        (head[1] || '').trim().toLowerCase() === 'score' &&
        rows.length >= 3

      if (scoreTable) {
        const ordered = rows
          .filter((r) => r[0] && r[1])
          .map((r) => ({ label: r[0].trim(), score: r[1].trim() }))

        out.push(
          <div
            className="md-scores"
            key={k++}
            style={{
              display: 'flex',
              gap: 12,
              width: '100%',
              overflowX: 'auto',
              paddingBottom: 8,
            }}
          >
            {ordered.map((r, ri) => (
              <div className="md-score-item" key={ri} style={{ flex: '0 0 190px', minWidth: 0 }}>
                <div className="score-label">{r.label}</div>
                <Cell text={r.score} />
              </div>
            ))}
          </div>
        )

        continue
      }
      /*
       * Normal markdown table
       */
      out.push(
        <div className="md-table-wrap" key={k++}>
          <table className="md-table">
            <thead>
              <tr>
                {head.map((c, ci) => (
                  <th key={ci}>
                    <Inline text={c} />
                  </th>
                ))}
              </tr>
            </thead>

            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>
                  {r.map((c, ci) => (
                    <td key={ci}>
                      <Cell text={c} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )

      continue
    }

    /*
     * Metadata blocks
     */
    if (
      META.test(line) ||
      (
        line.includes(' | ') &&
        line.split(' | ').every((p) => META.test(p.trim()))
      )
    ) {
      const pairs = []

      while (
        i < lines.length &&
        lines[i].trim() &&
        (
          META.test(lines[i]) ||
          lines[i].split(' | ').every((p) => META.test(p.trim()))
        )
      ) {
        lines[i].split(' | ').forEach((p) => {
          const m = p.trim().match(META)

          if (m) {
            pairs.push([m[1], m[2]])
          }
        })

        i++
      }

      out.push(
        <div className="md-meta" key={k++}>
          {pairs.map(([a, b], pi) => (
            <div className="kpi tone-info" key={pi}>
              <div className="kpi-label">{a}</div>
              <div className="md-meta-val">
                <Cell text={b} />
              </div>
            </div>
          ))}
        </div>
      )

      continue
    }

    /*
     * Lists
     */
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line)
      const items = []

      while (
        i < lines.length &&
        /^\s*([-*]|\d+\.)\s+/.test(lines[i])
      ) {
        items.push(
          lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, '')
        )
      }

      out.push(
        ordered
          ? (
            <ol className="list steps tone-cyan" key={k++}>
              {items.map((s, si) => (
                <li key={si}>
                  <span className="step-n">{si + 1}</span>
                  <span><Inline text={s} /></span>
                </li>
              ))}
            </ol>
          )
          : (
            <ul className="list tone-info" key={k++}>
              {items.map((s, si) => (
                <li key={si}>
                  <span className="dot" />
                  <span><Inline text={s} /></span>
                </li>
              ))}
            </ul>
          )
      )

      continue
    }

    /*
     * Paragraph
     */
    const buf = []

    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#|>|```|\||\s*([-*]|\d+\.)\s)/.test(lines[i])
    ) {
      buf.push(lines[i++])
    }

    if (buf.length === 0) {
      i++
      continue
    }

    out.push(
      <p className="md-p" key={k++}>
        <Inline text={buf.join(' ')} />
      </p>
    )
  }

  return <div className="md">{out}</div>
}

