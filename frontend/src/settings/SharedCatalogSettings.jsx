import { useEffect, useRef, useState } from "react"
import { apiFetch, downloadFile } from "../api"

export default function SharedCatalogSettings({ token }) {
  const [status, setStatus] = useState(null)
  const [query, setQuery] = useState("")
  const [matches, setMatches] = useState([])
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState("")
  const [pending, setPending] = useState("")
  const [reload, setReload] = useState(0)
  const operation = useRef(null)

  useEffect(() => {
    const controller = new AbortController()
    setStatus(null)
    setMatches([])
    setError(null)
    setNotice("")
    apiFetch("/catalog/status", { token, signal: controller.signal })
      .then((data) => { if (!controller.signal.aborted) setStatus(data) })
      .catch((failure) => { if (!controller.signal.aborted) setError(failure) })
    return () => controller.abort()
  }, [token, reload])

  useEffect(() => {
    setPending("")
    return () => {
      operation.current?.abort()
      operation.current = null
    }
  }, [token])

  async function act(action) {
    if (operation.current) return
    const controller = new AbortController()
    operation.current = controller
    setPending(action)
    setError(null)
    setNotice("")
    try {
      if (action === "source") {
        await downloadFile({ download_url: "/api/catalog/source", filename: "AIO3_Inventory.xlsx" }, token, controller.signal)
      } else if (action === "index") {
        const result = await apiFetch("/catalog/index", { method: "POST", token, signal: controller.signal })
        if (!controller.signal.aborted) {
          setStatus(result)
          setNotice(`${result.newly_indexed}개 상품의 AI 검색 자료를 준비했습니다.`)
        }
      } else {
        const result = await apiFetch(`/catalog/search?q=${encodeURIComponent(query.trim())}`, { token, signal: controller.signal })
        if (!controller.signal.aborted) {
          setMatches(result.matches)
          setNotice(`${result.matches.length}개 후보를 찾았습니다. 색상·옵션과 거래 단가를 확인해 주세요.`)
        }
      }
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure)
    } finally {
      if (operation.current === controller) {
        operation.current = null
        if (!controller.signal.aborted) setPending("")
      }
    }
  }

  const busy = Boolean(pending)
  return <div className="shared-catalog" aria-busy={busy}>
    <p className="section-note">팀 공용 상품과 첨부 재고표의 단가·수량을 조회합니다. 재고표 단가는 실제 거래의 매입·판매 단가인지 확인해야 합니다.</p>
    {error && <div role="alert" className="settings-load-error">
      <p>{error.message}</p>
      {error.code?.startsWith("OPENAI_KEY") && <a href="/settings">본인 AI 키 설정 열기</a>}
      <button type="button" className="btn" disabled={busy} onClick={() => setReload((n) => n + 1)}>상태 다시 읽기</button>
    </div>}
    {notice && <p role="status">{notice}</p>}
    {!status && !error && <p role="status">공용 상품을 확인하는 중입니다…</p>}
    {status && <>
      <p>상품 {status.products}개 · AI 검색 자료 {status.indexed}/{status.chunks}개 · 원본 {status.source_files}개</p>
      <div className="form-actions">
        <button className="btn" type="button" disabled={busy || !status.source_files} onClick={() => act("source")}>원본 엑셀 받기</button>
        <button className="btn" type="button" disabled={busy || status.indexed === status.chunks} onClick={() => act("index")}>
          {pending === "index" ? "검색 자료 준비 중…" : "AI 검색 자료 준비"}
        </button>
      </div>
      <p className="section-note">AI 검색 자료는 본인 OpenAI 계정으로 생성합니다. 처리 결과가 불명확하면 상태를 다시 읽어 확인하세요.</p>
      <form className="settings-form" onSubmit={(event) => { event.preventDefault(); act("search") }}>
        <label className="field"><span>모델코드 또는 제품명</span>
          <input value={query} onChange={(event) => setQuery(event.target.value)} maxLength={500} required />
        </label>
        <button className="btn btn-primary" disabled={busy || !query.trim()} type="submit">상품 검색</button>
      </form>
      {matches.length > 0 && <div className="shared-catalog-table"><table>
        <thead><tr><th>제품</th><th>옵션</th><th>재고표 단가</th><th>재고</th><th>출처</th></tr></thead>
        <tbody>{matches.map((item) => <tr key={item.sku}>
          <td>{item.product_name}<br /><small>{item.manufacturer_model_code || item.sku}</small></td>
          <td>{[item.color, item.switch_type].filter(Boolean).join(" / ") || "—"}</td>
          <td>{item.currency} {item.unit_price}</td><td>{item.stock_qty} {item.unit}</td>
          <td>{item.source.file} · {item.source.row}행</td>
        </tr>)}</tbody>
      </table></div>}
    </>}
  </div>
}
