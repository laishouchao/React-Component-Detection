import asyncio
import aiohttp
from bs4 import BeautifulSoup
import re
from typing import List, Dict, Optional
from playwright.async_api import async_playwright

# 修复：适配 React 18+ 新特征 + 重定向处理
REACT_FINGERPRINTS = {
    "core": {
        "global_vars": [
            "window.React",
            "window.ReactDOM",
            "window.__REACT_DEVTOOLS_GLOBAL_HOOK__",
            "window.ReactDOMClient",  # React 18+ 新增（createRoot 所在模块）
        ],
        "dom_attrs": [
            "data-reactroot",
            "data-reactid",
            "data-react-checksum",
            "data-react-server-components",  # React 18+ SSR 特征
        ],
        "js_url_patterns": [
            "react.js",
            "react-dom.js",
            "react.production.min.js",
            "react-dom.production.min.js",
            "chunk-react-",
            "vendors~react~",
            "jsx-runtime",  # React 18+ JSX 运行时
            "react-server",  # React 18+ 服务端组件
            "react-dom-client",  # React 18+ DOM 客户端模块
        ],
        "js_keywords": [
            "React.createElement",
            "jsx(",
            "jsxs(",
            "useState",
            "useEffect",
            "React.createRoot",  # React 18+ 核心渲染 API
            "ReactDOM.createRoot",  # React 18+ 兼容写法
            "react-router",
            "react-redux",
            "antd",
        ]
    },
    "auxiliary": [
        {"keywords": ["render()", "React.Component"], "desc": "React 类组件核心方法"},
        {"keywords": ["render()", "React.PureComponent"], "desc": "React 纯组件核心方法"},
        {"keywords": ["render()", "this.props"], "desc": "React 组件属性引用"},
        {"keywords": ["render()", "this.state"], "desc": "React 组件状态引用"},
        {"keywords": ["componentDidMount", "componentDidUpdate"], "desc": "React 生命周期方法"},
        {"keywords": ["createRoot", "React"], "desc": "React 18+ 渲染方法"},
    ]
}

class ReactDetector:
    def __init__(
        self,
        timeout: int = 10,
        concurrency: int = 5,
        use_playwright: bool = False,
        user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"  # 更新UA适配现代站点
    ):
        self.timeout = timeout
        self.concurrency = concurrency
        self.use_playwright = use_playwright
        self.user_agent = user_agent
        self.session_headers = {"User-Agent": self.user_agent}

    async def fetch_with_playwright(self, url: str) -> Dict[str, any]:
        """Playwright 模式：自动跟随重定向，适配 React 18+"""
        result = {"html": None, "js_urls": [], "global_vars": [], "redirect_url": None, "error": None}
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(user_agent=self.user_agent)
                # 监听重定向，记录最终URL
                page.on("framenavigated", lambda frame: setattr(result, "redirect_url", frame.url) if frame == page.main_frame else None)
                await page.goto(url, timeout=self.timeout * 1000)
                await page.wait_for_load_state("networkidle")
                
                result["html"] = await page.content()
                result["redirect_url"] = page.url  # 记录最终跳转后的URL
                js_urls = await page.eval_on_selector_all("script[src]", "els => els.map(el => el.src)")
                result["js_urls"] = [url for url in js_urls if url]
                
                # 检测核心全局变量（含 React 18+ 新增）
                for var in REACT_FINGERPRINTS["core"]["global_vars"]:
                    exists = await page.evaluate(f"typeof {var} !== 'undefined'")
                    if exists:
                        result["global_vars"].append(var)
                
                await browser.close()
        except Exception as e:
            result["error"] = f"Playwright 加载失败: {str(e)}"
        return result

    async def fetch_page(self, session: aiohttp.ClientSession, url: str) -> Dict[str, any]:
        """aiohttp 模式：开启重定向跟随，适配 React 18+"""
        result = {"html": None, "js_urls": [], "global_vars": [], "redirect_url": None, "error": None}
        try:
            # 关键修复：allow_redirects=True 自动跟随重定向
            async with session.get(url, timeout=self.timeout, allow_redirects=True) as response:
                result["redirect_url"] = str(response.url)  # 记录最终跳转后的URL
                if response.status != 200:
                    result["error"] = f"HTTP状态码异常: {response.status}（最终URL：{result['redirect_url']}）"
                    return result
                html = await response.text()
                result["html"] = html
                
                soup = BeautifulSoup(html, "html.parser")
                script_tags = soup.find_all("script")
                for script in script_tags:
                    js_url = script.get("src")
                    if js_url:
                        from urllib.parse import urljoin
                        js_url = urljoin(result["redirect_url"], js_url)  # 使用跳转后的URL补全相对路径
                        result["js_urls"].append(js_url)
        except Exception as e:
            result["error"] = f"页面加载失败: {str(e)}"
        return result

    async def check_js_content(self, session: aiohttp.ClientSession, js_url: str) -> Dict[str, List[str]]:
        """检查JS内容（适配 React 18+ 关键词）"""
        result = {"core": [], "auxiliary": []}
        try:
            async with session.get(js_url, timeout=self.timeout, allow_redirects=True) as response:  # JS文件也可能重定向
                if response.status != 200:
                    return result
                js_content = await response.text()
                js_lower = js_content.lower()

                # 检测核心JS关键词（含 React 18+）
                for keyword in REACT_FINGERPRINTS["core"]["js_keywords"]:
                    if re.search(re.escape(keyword.lower()), js_lower):
                        result["core"].append(f"JS源码含React核心API: {keyword}")

                # 检测辅助证据组（含 React 18+）
                for group in REACT_FINGERPRINTS["auxiliary"]:
                    group_kws = [kw.lower() for kw in group["keywords"]]
                    if all(re.search(re.escape(kw), js_lower) for kw in group_kws):
                        result["auxiliary"].append(f"{group['desc']}（匹配：{', '.join(group['keywords'])}）")
        except (asyncio.TimeoutError, aiohttp.ClientError):
            pass
        except Exception:
            pass
        return result

    async def detect_single_url(self, url: str) -> Dict[str, any]:
        """核心探测逻辑（含重定向提示）"""
        result = {
            "url": url,
            "final_url": url,  # 最终访问的URL（含重定向）
            "is_react": False,
            "is_suspected": False,
            "core_evidence": [],
            "aux_evidence": [],
            "error": None
        }

        # 1. 获取页面数据（开启重定向跟随）
        if self.use_playwright:
            page_data = await self.fetch_with_playwright(url)
        else:
            async with aiohttp.ClientSession(headers=self.session_headers) as session:
                page_data = await self.fetch_page(session, url)

        if page_data["error"]:
            result["error"] = page_data["error"]
            return result

        # 记录最终跳转后的URL
        if page_data["redirect_url"]:
            result["final_url"] = page_data["redirect_url"]

        # 2. 检测核心证据（含 React 18+ 特征）
        # 2.1 全局变量
        for var in page_data.get("global_vars", []):
            result["core_evidence"].append(f"[核心] 存在React全局变量: {var}")
        
        # 2.2 DOM属性
        if page_data["html"]:
            soup = BeautifulSoup(page_data["html"], "html.parser")
            for attr in REACT_FINGERPRINTS["core"]["dom_attrs"]:
                if soup.find(attrs={attr: True}):
                    result["core_evidence"].append(f"[核心] DOM含React专属属性: {attr}")
        
        # 2.3 JS URL特征（含 React 18+）
        for js_url in page_data["js_urls"]:
            for pattern in REACT_FINGERPRINTS["core"]["js_url_patterns"]:
                if pattern.lower() in js_url.lower():
                    result["core_evidence"].append(f"[核心] JS URL含React特征: {js_url}（匹配：{pattern}）")
        
        # 2.4 JS内容核心关键词（含 React 18+）
        if page_data["js_urls"]:
            async with aiohttp.ClientSession(headers=self.session_headers) as session:
                js_tasks = [self.check_js_content(session, js_url) for js_url in page_data["js_urls"]]
                js_results = await asyncio.gather(*js_tasks, return_exceptions=False)
                for js_res in js_results:
                    result["core_evidence"].extend(js_res["core"])
                    result["aux_evidence"].extend(js_res["auxiliary"])

        # 3. 去重证据
        result["core_evidence"] = list(dict.fromkeys(result["core_evidence"]))
        result["aux_evidence"] = list(dict.fromkeys(result["aux_evidence"]))

        # 4. 判定逻辑
        core_count = len(result["core_evidence"])
        aux_count = len(result["aux_evidence"])
        
        if core_count >= 1:
            result["is_react"] = True
        elif aux_count >= 2:
            result["is_react"] = True
        elif aux_count == 1:
            result["is_suspected"] = True

        return result

    async def detect_batch_urls(self, urls: List[str]) -> List[Dict[str, any]]:
        """批量探测（控制并发）"""
        semaphore = asyncio.Semaphore(self.concurrency)
        
        async def bounded_detect(url: str) -> Dict[str, any]:
            async with semaphore:
                return await self.detect_single_url(url)

        tasks = [bounded_detect(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return results

    def run_batch_from_file(self, file_path: str) -> List[Dict[str, any]]:
        """从文件读取URL批量探测"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                urls = [line.strip() for line in f if line.strip().startswith(("http://", "https://"))]
            if not urls:
                raise ValueError("文件中无有效URL（需以http/https开头）")
            return asyncio.run(self.detect_batch_urls(urls))
        except Exception as e:
            print(f"读取文件失败: {str(e)}")
            return []

def print_results(results: List[Dict[str, any]]):
    """格式化输出（含重定向提示）"""
    print("=" * 80)
    print(f"React资产探测结果汇总 (共{len(results)}个URL)")
    print("=" * 80)
    for res in results:
        print(f"\n[原始URL]: {res['url']}")
        if res["final_url"] != res["url"]:
            print(f"[最终URL]: {res['final_url']}（已自动跟随重定向）")
        if res["error"]:
            print(f"  状态: 探测失败")
            print(f"  错误: {res['error']}")
        else:
            if res["is_react"]:
                status = "✅ 使用React"
            elif res["is_suspected"]:
                status = "⚠️  未使用React（疑似但证据不足）"
            else:
                status = "❌ 未使用React"
            print(f"  状态: {status}")

            if res["core_evidence"]:
                print(f"  核心证据 ({len(res['core_evidence'])}条):")
                for idx, evi in enumerate(res["core_evidence"], 1):
                    print(f"    {idx}. {evi}")
            
            if res["aux_evidence"]:
                print(f"  辅助证据 ({len(res['aux_evidence'])}条):")
                for idx, evi in enumerate(res["aux_evidence"], 1):
                    print(f"    {idx}. {evi}")
            
            if not res["core_evidence"] and not res["aux_evidence"]:
                print(f"  证据链: 无任何React相关特征")
        print("-" * 50)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="React资产探测工具 - 修复重定向+适配React18+")
    parser.add_argument("-u", "--url", type=str, help="单个URL探测（例：https://reactjs.org）")
    parser.add_argument("-f", "--file", type=str, help="批量探测（文件路径，每行一个URL）")
    parser.add_argument("-t", "--timeout", type=int, default=10, help="请求超时时间（默认10秒）")
    parser.add_argument("-c", "--concurrency", type=int, default=5, help="批量并发数（默认5）")
    parser.add_argument("-p", "--playwright", action="store_true", help="使用Playwright精准模式（支持SSR/动态渲染）")
    args = parser.parse_args()

    detector = ReactDetector(
        timeout=args.timeout,
        concurrency=args.concurrency,
        use_playwright=args.playwright
    )

    if args.url:
        result = asyncio.run(detector.detect_single_url(args.url))
        print_results([result])
    elif args.file:
        results = detector.run_batch_from_file(args.file)
        print_results(results)
    else:
        parser.print_help()