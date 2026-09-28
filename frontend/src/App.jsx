import { useState, useEffect } from 'react'
import { ScoreHero, ScoreGrid, DynamicTesting, RuntimeAuthDbEvidence, PlagiarismCheck, Feedback, AiReview, MarkdownView } from './Report.jsx'

// Works whether you open the dashboard as localhost or via another host/IP.
const API_BASE = `${window.location.protocol}//${window.location.hostname}:8000`

async function api(apiKey, path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { 'X-API-Key': apiKey, ...(options.headers || {}) },
  })
  if (!res.ok) {
    let detail = `Request failed (${res.status})`
    try {
      const body = await res.json()
      if (body.detail) {
        // FastAPI validation errors arrive as a list of {msg, loc}; show just the messages.
        detail = typeof body.detail === 'string'
          ? body.detail
          : Array.isArray(body.detail)
            ? body.detail.map((d) => (d.msg || '').replace(/^Value error, /, '')).filter(Boolean).join('; ')
            : JSON.stringify(body.detail)
      }
    } catch { /* not JSON */ }
    if (res.status === 401) detail = 'Invalid API key - click "Change Key" and enter the current one.'
    throw new Error(detail)
  }
  return res
}

function useApiKey() {
  const [key, setKey] = useState(localStorage.getItem('sandbox_api_key') || '')
  const save = (k) => {
    localStorage.setItem('sandbox_api_key', k)
    setKey(k)
  }
  return [key, save]
}

const SCORE_ITEMS = [
  ['engineering_maturity', 'Engineering Maturity'],
  ['feature_completion', 'Feature Completion'],
  ['code_quality', 'Code Quality'],
  ['architecture', 'Architecture'],
  ['security', 'Security'],
  ['api_quality', 'API Quality'],
  ['deployment_readiness', 'Deployment Readiness'],
  ['structure', 'Project Structure'],
  ['database_connectivity', 'Database Connectivity'],
  ['authentication_flow', 'Authentication Flow'],
  ['error_handling', 'Error Handling'],
  ['security_configuration', 'Security Configuration'],
  ['required_features', 'Required Features'],
  ['environment_configuration', 'Environment Config'],
  ['documentation', 'Documentation'],
]

// Build logs contain terminal colour codes (ESC[91m ...) - strip them for display.
const stripAnsi = (text) => (text || '').replace(/\x1b\[[0-9;]*[A-Za-z]/g, '')

// Older API versions sent timestamps without a timezone marker (UTC time that browsers then
// showed as local time, hours off). Treat a marker-less timestamp as UTC.
const parseDate = (iso) => (iso ? new Date(/(Z|[+-]\d\d:?\d\d)$/i.test(iso) ? iso : `${iso}Z`) : null)
const formatDate = (iso) => (iso ? parseDate(iso).toLocaleString() : '-')

// "https://github.com/user/repo/" or ".../repo.git" -> "user/repo"
const shortRepo = (url) => {
  if (url.startsWith('upload:') || url.startsWith('image:')) return url
  return url.replace(/\/+$/, '').replace(/\.git$/, '').split('/').slice(-2).join('/')
}

const TAB_ICONS = { submit: '\u2191', results: '\u2630', leaderboard: '\u2605', metrics: '\u25F7' }

const JOB_STATUS = { PENDING: 'queued', STARTED: 'running', SUCCESS: 'done', FAILURE: 'failed' }

function scoreClass(value) {
  if (value === null || value === undefined) return 'na'
  if (value >= 80) return 'good'
  if (value >= 50) return 'warn'
  return 'bad'
}

// Heavy (runtime) auth/DB probe evidence (see backend/app/sandbox_engine.py::_apply_auth_db_probe_evidence).
// Additive-only: shown only when the probe actually found something; a guess that matched nothing leaves
// this section absent and the scores untouched.
// Bonus: AI Plagiarism Detection. Never changes any score - purely a mentor-facing signal.
// Renders nothing when the check didn't run (empty repo, first submission of its type) or
// found nothing worth surfacing (see thresholds in backend/app/plagiarism.py).
const FEEDBACK_SECTIONS = [
  ['strengths', 'Strengths'],
  ['weaknesses', 'Weaknesses'],
  ['missing_requirements', 'Missing Requirements'],
  ['security_risks', 'Security Risks'],
  ['performance_suggestions', 'Performance Suggestions'],
  ['refactoring_suggestions', 'Refactoring Suggestions'],
  ['improvement_roadmap', 'Improvement Roadmap'],
]

function GithubMeta({ meta }) {
  if (!meta || !meta.available) return null
  return (
    <div className="kv-row small">
      <span>Stars {meta.stars ?? '-'}</span>
      <span>Forks {meta.forks ?? '-'}</span>
      <span>Open issues {meta.open_issues ?? '-'}</span>
      {meta.language && <span>{meta.language}</span>}
      {meta.last_pushed_at && <span>Last push {new Date(meta.last_pushed_at).toLocaleDateString()}</span>}
      {meta.description && <div style={{ width: '100%' }}>{meta.description}</div>}
    </div>
  )
}

function ReportActions({ reportOpen, loading, error, onToggle, onDownload }) {
  return (
    <div className="report-actions">
      <div className="row" style={{ marginBottom: 0 }}>
        <button className="secondary" onClick={onToggle} disabled={loading}>
          {loading ? 'Preparing report...' : reportOpen ? 'Back to result details' : 'View report'}
        </button>
        <button className="secondary" onClick={onDownload} disabled={loading}>Download report (.md)</button>

      </div>
      {error && <div className="banner error">{error}</div>}
    </div>
  )
}
function ReevaluateButton({ apiKey, submissionId, repoUrl }) {
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const isUpload = repoUrl?.startsWith('upload:')

  const run = async () => {
    setBusy(true)
    setMsg('')
    try {
      await api(apiKey, `/results/${submissionId}/reevaluate`, { method: 'POST' })
      setMsg('Queued - press Refresh in a minute to see the new result.')
    } catch (e) {
      setMsg(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="row" style={{ marginTop: 8, alignItems: 'center' }}>
      <button
        className="secondary"
        onClick={run}
        disabled={busy || isUpload}
        title={isUpload ? 'Uploaded ZIPs are deleted after evaluation - upload again' : 'Run a fresh evaluation of this repository'}
      >
        {busy ? 'Queuing...' : 'Re-evaluate'}
      </button>
      {msg && <span className="small">{msg}</span>}
    </div>
  )
}

// Results-list rows only carry scores; fetch the full record so failed/build_error rows can show
// their error message and build/run logs (that is where the reason for a failure is).
function ExpandedResult({ apiKey, submissionId, scores, repoUrl }) {
  const [full, setFull] = useState(null)
  const [err, setErr] = useState('')
  const [reportOpen, setReportOpen] = useState(false)

  useEffect(() => {
    api(apiKey, `/results/${submissionId}`).then((r) => r.json()).then(setFull).catch((e) => setErr(e.message))
  }, [apiKey, submissionId])

  return (
    <>
      {err && <div className="banner error">{err}</div>}
      {full?.error && <div className="banner error">{full.error}</div>}
      <ResultDetail apiKey={apiKey} submissionId={submissionId} scores={scores} repoUrl={repoUrl}
        reportOpen={reportOpen} onReportOpenChange={setReportOpen} />
      {!reportOpen && full?.logs && (
        <details style={{ marginTop: 12 }}>
          <summary className="small">Build / run logs</summary>
          <pre className="report">{stripAnsi(full.logs)}</pre>
        </details>
      )}
    </>
  )
}

function ResultDetail({ apiKey, submissionId, scores, repoUrl, reportOpen, onReportOpenChange }) {
  const [reportText, setReportText] = useState('')
  const [reportError, setReportError] = useState('')
  const [reportLoading, setReportLoading] = useState(false)

  const fetchReport = async () => {
    const res = await api(apiKey, `/results/${submissionId}/report`)
    return res.text()
  }

  const toggleReport = async () => {
    setReportError('')
    if (reportOpen) {
      onReportOpenChange(false)
      return
    }
    setReportLoading(true)
    try {
      setReportText(await fetchReport())
      onReportOpenChange(true)
    } catch (e) {
      setReportError(e.message)
    } finally {
      setReportLoading(false)
    }
  }

  const downloadReport = async () => {
    setReportError('')
    try {
      const text = await fetchReport()
      const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown' }))
      const a = document.createElement('a')
      a.href = url
      a.download = `report-${submissionId}.md`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setReportError(e.message)
    }
  }

  return (
    <>
      <ReportActions reportOpen={reportOpen} loading={reportLoading} error={reportError}
        onToggle={toggleReport} onDownload={downloadReport} />
      {reportOpen ? (
        <div className="card share-report">
          <div className="small report-caption">Shareable evaluation summary · detailed evidence remains in result details</div>
          <MarkdownView text={reportText} />
        </div>
      ) : (
        <>
          {scores?.project_root_note && <div className="banner warn">{scores.project_root_note}</div>}
          {scores?.project_type_note && <div className="banner warn">{scores.project_type_note}</div>}
          {scores?.library_note && <div className="banner warn">{scores.library_note}</div>}
          {scores?.env_injected?.length > 0 && (
            <div className="banner">.env.example values injected: {scores.env_injected.join(', ')}</div>
          )}
          <GithubMeta meta={scores?.github_metadata} />
          <ScoreHero scores={scores} />
          <ScoreGrid scores={scores} />
          <DynamicTesting scores={scores} />
          <RuntimeAuthDbEvidence scores={scores} />
          <PlagiarismCheck scores={scores} />
          <Feedback feedback={scores?.feedback} />
          <AiReview review={scores?.ai_review} />
          {repoUrl && <ReevaluateButton apiKey={apiKey} submissionId={submissionId} repoUrl={repoUrl} />}
        </>
      )}
    </>
  )
}
// Optional model-written review. Clearly labelled: it is advisory and never changes a score.
function SubmitTab({ apiKey, job, setJob }) {
  const [mode, setMode] = useState('url')
  const [repoUrl, setRepoUrl] = useState('')
  const [imageRef, setImageRef] = useState('')
  const [projectType, setProjectType] = useState('python')
  const [file, setFile] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [reportOpen, setReportOpen] = useState(false)

  const canSubmit =
    (mode === 'url' && repoUrl.trim()) || (mode === 'zip' && file) || (mode === 'image' && imageRef.trim())

  const submit = async () => {
    setSubmitting(true)
    setError('')
    try {
      let res
      let label
      if (mode === 'url') {
        label = repoUrl.trim()
        res = await api(apiKey, '/evaluate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ repo_url: label, project_type: projectType }),
        })
      } else if (mode === 'zip') {
        label = file.name
        const form = new FormData()
        form.append('file', file)
        form.append('project_type', projectType)
        res = await api(apiKey, '/evaluate/upload', { method: 'POST', body: form })
      } else {
        label = `image:${imageRef.trim()}`
        res = await api(apiKey, '/evaluate/docker-image', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ image: imageRef.trim() }),
        })
      }
      const data = await res.json()
      setReportOpen(false)
      setJob({ taskId: data.task_id, status: 'queued', result: null, error: '', repoLabel: label })
    } catch (e) {
      setError(e.message)
    } finally {
      setSubmitting(false)
    }
  }

  const result = job.result

  return (
    <div>
      <div className="card">
        <div className="row">
          <label><input type="radio" checked={mode === 'url'} onChange={() => setMode('url')} /> GitHub / GitLab URL</label>
          <label><input type="radio" checked={mode === 'zip'} onChange={() => setMode('zip')} /> ZIP Upload</label>
          <label><input type="radio" checked={mode === 'image'} onChange={() => setMode('image')} /> Docker Image</label>
        </div>

        {mode === 'url' && (
          <div className="row">
            <input style={{ flex: 1 }} placeholder="https://github.com/user/repo  (public repos)" value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} />
          </div>
        )}
        {mode === 'zip' && (
          <div className="row">
            <input type="file" accept=".zip" onChange={(e) => setFile(e.target.files[0] || null)} />
            <span className="small">Max 50 MB. Leave out node_modules / .git / venv.</span>
          </div>
        )}
        {mode === 'image' && (
          <div className="row">
            <input style={{ flex: 1 }} placeholder="user/image:tag" value={imageRef} onChange={(e) => setImageRef(e.target.value)} />
          </div>
        )}

        <div className="row">
          {mode !== 'image' && (
            <select value={projectType} onChange={(e) => setProjectType(e.target.value)}>
              <option value="python">Python</option>
              <option value="node">Node.js</option>
              <option value="php">PHP/Laravel</option>
            </select>
          )}
          <button onClick={submit} disabled={submitting || !canSubmit}>
            {submitting ? 'Submitting...' : 'Evaluate'}
          </button>
        </div>
        {mode === 'image' && (
          <div className="small">Docker images are health-checked only - no source code, so static scores show N/A.</div>
        )}
        {error && <div className="banner error">{error}</div>}
        {job.taskId && (
          <div className="small">
            {job.repoLabel} - Status: {job.result ? `finished, result: ${job.result.status}` : (JOB_STATUS[job.status] || job.status)}
          </div>
        )}
        {job.error && <div className="banner error">{job.error}</div>}
      </div>

      {result && (
        <div className="card">
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <b>{result.repo_url}</b>
            <span className={`badge ${result.status}`}>{result.status}</span>
          </div>
          <div className="small">
            Build: {String(result.build_success)} | Execution: {String(result.execution_success)} | Duration: {result.duration_seconds}s
          </div>
          {result.error && <div className="banner error">{result.error}</div>}
          <ResultDetail apiKey={apiKey} submissionId={result.submission_id} scores={result.scores} repoUrl={result.repo_url}
            reportOpen={reportOpen} onReportOpenChange={setReportOpen} />
          {result.logs && !reportOpen && (
            <details style={{ marginTop: 12 }}>
              <summary className="small">Build / run logs</summary>
              <pre className="report">{stripAnsi(result.logs)}</pre>
            </details>
          )}
        </div>
      )}
    </div>
  )
}

function ResultsTab({ apiKey }) {
  const [results, setResults] = useState([])
  const [expanded, setExpanded] = useState(null)
  const [limit, setLimit] = useState(20)
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [reloadTick, setReloadTick] = useState(0)

  useEffect(() => {
    setLoading(true)
    setError('')
    api(apiKey, `/results?limit=${limit}`)
      .then((r) => r.json())
      .then(setResults)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [apiKey, limit, reloadTick])

  const q = query.trim().toLowerCase()
  const shown = results.filter(
    (r) => !q || r.repo_url.toLowerCase().includes(q) || r.status.toLowerCase().includes(q) || r.submission_id.includes(q),
  )

  return (
    <div>
      <div className="row results-toolbar">
        <input style={{ flex: 1 }} placeholder="Search by repository, status, or ID..." value={query} onChange={(e) => setQuery(e.target.value)} />
        <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
          <option value={20}>Latest 20</option>
          <option value={50}>Latest 50</option>
          <option value={100}>Latest 100</option>
          <option value={500}>Latest 500</option>
        </select>
        <button className="secondary" onClick={() => setReloadTick((t) => t + 1)}>Refresh</button>
      </div>
      {error && <div className="banner error">{error}</div>}
      {loading && <div className="loader" role="status" aria-label="Loading" />}
      {shown.map((r) => {
        const maturityEntry = r.scores?.engineering_maturity
        const maturity = maturityEntry?.score
        const partial = maturityEntry?.partial
        return (
          <div className="card result-card" key={r.submission_id}>
            <div
              className="row"
              style={{ justifyContent: 'space-between', cursor: 'pointer', marginBottom: 4 }}
              onClick={() => setExpanded(expanded === r.submission_id ? null : r.submission_id)}
            >
              <b>{r.repo_url}</b>
              <span>
                {r.status === 'success' && maturity !== undefined && maturity !== null && (
                  partial
                    ? <span className="pill" title={`Only ${maturityEntry.components_averaged} check(s) available`}>health check only</span>
                    : <span className={`pill ${scoreClass(maturity)}`}>{maturity}/100</span>
                )}{' '}
                <span className={`badge ${r.status}`}>{r.status}</span>
              </span>
            </div>
            <div className="small">{formatDate(r.created_at)} - ID {r.submission_id}</div>
            {expanded === r.submission_id && (
              <ExpandedResult apiKey={apiKey} submissionId={r.submission_id} scores={r.scores} repoUrl={r.repo_url} />
            )}
          </div>
        )
      })}
      {!loading && !error && shown.length === 0 && (
        <p className="small">{results.length === 0 ? 'No submissions yet.' : 'No submissions match your search.'}</p>
      )}
    </div>
  )
}

function LeaderboardTab({ apiKey }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api(apiKey, '/leaderboard').then((r) => r.json()).then(setData).catch((e) => setError(e.message))
  }, [apiKey])

  if (error) return <div className="banner error">{error}</div>
  if (!data) return <div className="loader" role="status" aria-label="Loading" />

  const sections = [
    ['highest_engineering_score', 'Highest Engineering Score', ''],
    ['fastest_build', 'Fastest Build (image build time)', 's'],
    ['best_architecture', 'Best Architecture', ''],
    ['best_api_design', 'Best API Design', ''],
    ['best_documentation', 'Best Documentation', ''],
    ['best_performance', 'Best Performance (avg latency)', 'ms'],
  ]

  return (
    <>
    <p className="small" style={{ marginTop: 0 }}>
      Ranking uses each repository&apos;s latest successful evaluation. Hover a row for its evaluation date.
      Docker-image submissions are health-checked only, so they are not ranked for overall score.
    </p>
    <div className="score-grid" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))' }}>
      {sections.map(([key, label, unit]) => (
        <div className="card" key={key}>
          <b>{label}</b>
          {(data[key] || []).map((row, i) => (
            <div className="leaderboard-row" key={i}>
              <span className="leaderboard-name" title={`${shortRepo(row.repo_url)} · Evaluated ${formatDate(row.evaluated_at)}`}>{i + 1}. {shortRepo(row.repo_url)}</span>
              <span>{row.value}{unit}</span>
            </div>
          ))}
          {(!data[key] || data[key].length === 0) && <p className="small">No data yet</p>}
        </div>
      ))}
    </div>
    </>
  )
}

function MetricsTab({ apiKey }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    const load = () => api(apiKey, '/metrics').then((r) => r.json()).then((d) => { setData(d); setError('') }).catch((e) => setError(e.message))
    load()
    const t = setInterval(load, 10000) // keep queue depth / counts live while jobs run
    return () => clearInterval(t)
  }, [apiKey])

  if (error) return <div className="banner error">{error}</div>
  if (!data) return <div className="loader" role="status" aria-label="Loading" />

  return (
    <div className="metric-grid">
      <div className="card">
        <div className="metric-num">{data.total_submissions}</div>
        <div className="metric-label">Total Submissions</div>
      </div>
      {data.completion_rate_percent !== undefined && (
        <div
          className="card"
          title="Evaluations that ended with a verdict. Only internal errors, worker interruptions and time-limit stops count against it - a missing repository or a build error is a correct verdict."
        >
          <div className="metric-num">{data.completion_rate_percent}%</div>
          <div className="metric-label">Evaluations completed (platform reliability)</div>
        </div>
      )}
      <div className="card" title="Submissions that built and ran. Deliberately broken test submissions are included.">
        <div className="metric-num">{data.success_rate_percent}%</div>
        <div className="metric-label">Submissions that built and ran</div>
      </div>
      <div className="card">
        <div className="metric-num">
          {(data.avg_success_duration_seconds ?? data.avg_duration_seconds) != null
            ? `${data.avg_success_duration_seconds ?? data.avg_duration_seconds}s`
            : '-'}
        </div>
        <div className="metric-label">Avg Duration (successful runs)</div>
      </div>
      <div className="card">
        <div className="metric-num">{data.queue_depth ?? '-'}</div>
        <div className="metric-label">Queued (waiting for a worker)</div>
      </div>
      {data.completion_rate_percent !== undefined && (
        <div className="small" style={{ gridColumn: '1 / -1' }}>
          {data.platform_error_count} of {data.total_submissions} evaluation(s) ended with a platform error (internal error,
          worker restart or time limit). A failed clone, a build error or an unreachable app is a correct verdict, so it
          counts as completed - only the second number reflects how many submissions actually passed.
        </div>
      )}
      {data.status_breakdown && Object.keys(data.status_breakdown).length > 0 && (
        <div className="card" style={{ gridColumn: '1 / -1' }}>
          <div className="metric-label" style={{ marginTop: 0, marginBottom: 8 }}>Status breakdown</div>
          <div className="row" style={{ marginBottom: 0 }}>
            {Object.entries(data.status_breakdown).map(([status, count]) => (
              <span key={status}><span className={`badge ${status}`}>{status}</span> {count}</span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// Bonus: "Public Engineering Scorecard" - a read-only page at /public that needs no API
// key, so the link can be shared with anyone (other interns, recruiters, mentors).
// Deliberately reuses the same rendering as the authenticated leaderboard tab so the two
// never visually drift apart; the only difference is the (unauthenticated) data source
// and that this is the top-level page, not a tab inside the dashboard.
function PublicScorecard() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    fetch(`${API_BASE}/public/scorecard`)
      .then((r) => {
        if (!r.ok) throw new Error(`Request failed (${r.status})`)
        return r.json()
      })
      .then(setData)
      .catch((e) => setError(e.message))
  }, [])

  const sections = [
    ['highest_engineering_score', 'Highest Engineering Score', ''],
    ['fastest_build', 'Fastest Build (image build time)', 's'],
    ['best_architecture', 'Best Architecture', ''],
    ['best_api_design', 'Best API Design', ''],
    ['best_documentation', 'Best Documentation', ''],
    ['best_performance', 'Best Performance (avg latency)', 'ms'],
  ]

  return (
    <div className="app">
      <header>
        <h1>Ezitech Engineering Sandbox</h1>
        <span className="pill">Public Scorecard &middot; no login needed</span>
      </header>
      <p className="small" style={{ marginTop: 0 }}>
        Top 5 per category, ranked by each project&apos;s latest successful evaluation. This page is
        read-only and shareable - it needs no API key. Docker-image submissions are health-checked only,
        so they are not ranked for overall score.
      </p>
      {error && <div className="banner error">{error}</div>}
      {!data && !error && <div className="loader" role="status" aria-label="Loading" />}
      {data && (
        <div className="score-grid" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))' }}>
          {sections.map(([key, label, unit]) => (
            <div className="card" key={key}>
              <b>{label}</b>
              {(data[key] || []).map((row, i) => (
                <div className="leaderboard-row" key={i}>
                  <span className="leaderboard-name" title={`${shortRepo(row.repo_url)} · Evaluated ${formatDate(row.evaluated_at)}`}>{i + 1}. {shortRepo(row.repo_url)}</span>
                  <span>{row.value}{unit}</span>
                </div>
              ))}
              {(!data[key] || data[key].length === 0) && <p className="small">No data yet</p>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function App() {
  if (window.location.pathname.replace(/\/+$/, '') === '/public') {
    return <PublicScorecard />
  }

  const [apiKey, setApiKey] = useApiKey()
  const [tab, setTab] = useState('submit')
  const [keyInput, setKeyInput] = useState('')
  const [keyError, setKeyError] = useState('')
  const [checkingKey, setCheckingKey] = useState(false)
  const [job, setJob] = useState({ taskId: null, status: null, result: null, error: '' })

  // Polling lives at App level so it keeps running when switching tabs.
  useEffect(() => {
    if (!apiKey || !job.taskId || job.status === 'SUCCESS' || job.status === 'FAILURE') return
    let failures = 0
    const t = setInterval(async () => {
      try {
        const res = await api(apiKey, `/tasks/${job.taskId}`)
        const data = await res.json()
        failures = 0
        if (data.status === 'SUCCESS' && data.result) {
          const full = await (await api(apiKey, `/results/${data.result.submission_id}`)).json()
          setJob((prev) => ({ ...prev, status: 'SUCCESS', result: full }))
        } else if (data.status === 'FAILURE') {
          setJob((prev) => ({ ...prev, status: 'FAILURE', error: `The evaluation task crashed${data.error ? `: ${data.error}` : ''}. Details: docker compose logs worker` }))
        } else {
          setJob((prev) => ({ ...prev, status: data.status }))
        }
      } catch (e) {
        failures += 1
        if (failures >= 5) {
          setJob((prev) => ({ ...prev, status: 'FAILURE', error: `Lost contact with the API: ${e.message}` }))
        }
      }
    }, 3000)
    return () => clearInterval(t)
  }, [apiKey, job.taskId, job.status])

  const saveKey = async () => {
    const k = keyInput.trim()
    if (!k) return
    setCheckingKey(true)
    setKeyError('')
    try {
      await api(k, '/metrics') // fails with 401 for a wrong key - tell the user now, not on the next tab
      setApiKey(k)
      setKeyInput('')
    } catch (e) {
      setKeyError(e.message)
    } finally {
      setCheckingKey(false)
    }
  }

  if (!apiKey) {
    return (
      <div className="app login-wrap">
        <div className="card login">
          <div className="logo">E</div>
          <h1>Enter API Key</h1>
          <p className="small">Enter your API key to access the sandbox dashboard.</p>
          <div className="row" style={{ marginTop: 12 }}>
            <input
              style={{ flex: 1 }} type="password" autoFocus value={keyInput} placeholder="X-API-Key"
              onChange={(e) => setKeyInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && saveKey()}
            />
            <button onClick={saveKey} disabled={checkingKey || !keyInput.trim()}>{checkingKey ? 'Checking...' : 'Save'}</button>
          </div>
          {keyError && <div className="banner error">{keyError}</div>}
        </div>
      </div>
    )
  }

  return (
    <div className="app">
      <header>
        <div className="brand">
          <div className="logo">E</div>
          <div>
            <h1>Ezitech Engineering Sandbox</h1>
            <div className="brand-sub">Automated code evaluation &middot; scores &middot; insights</div>
          </div>
        </div>
        <button className="secondary" onClick={() => setApiKey('')}>Change Key</button>
      </header>
      <div className="tabs">
        {['submit', 'results', 'leaderboard', 'metrics'].map((t) => (
          <button key={t} className={`tab ${tab === t ? 'active' : ''}`} onClick={() => setTab(t)}>
            <span className="tab-ico">{TAB_ICONS[t]}</span>{t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>
      <div style={{ display: tab === 'submit' ? 'block' : 'none' }}>
        <SubmitTab apiKey={apiKey} job={job} setJob={setJob} />
      </div>
      {tab === 'results' && <ResultsTab apiKey={apiKey} />}
      {tab === 'leaderboard' && <LeaderboardTab apiKey={apiKey} />}
      {tab === 'metrics' && <MetricsTab apiKey={apiKey} />}
    </div>
  )
}
