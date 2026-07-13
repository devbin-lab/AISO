import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'

interface Props {
  /** 렌더링할 마크다운 원문 (모델 답변) */
  text: string
}

/** 어시스턴트 답변의 마크다운(굵게·제목·목록·표·코드·링크 등)을 렌더링한다.
 *
 * - react-markdown 기본값은 raw HTML을 렌더하지 않고 위험한 URL(javascript: 등)을 차단하므로,
 *   모델이 검색 결과의 신뢰할 수 없는 문자열을 그대로 옮겨도 XSS로 이어지지 않는다.
 * - 링크는 target=_blank로 열어 메인 프로세스의 setWindowOpenHandler가 기본 브라우저로 연다
 *   (앱 창이 외부 사이트로 이탈하는 것을 방지). GFM(표·취소선·자동링크)은 remark-gfm으로 지원.
 */
function Markdown({ text }: Props): React.JSX.Element {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
          // 넓은 표가 말풍선을 넘치지 않도록 가로 스크롤 컨테이너로 감싼다.
          table: ({ children }) => (
            <div className="md-table">
              <table>{children}</table>
            </div>
          )
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}

export default Markdown
