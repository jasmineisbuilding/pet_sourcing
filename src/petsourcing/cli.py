"""Command line interface: `petsrc --help`."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import func, select

from . import config
from .db import init_db, make_engine, session_scope
from .economics import load_scenarios, sensitivity
from .ingest import IMPORTERS, ImportReport
from .claims import Citation, add_claim, claims_citing_superseded
from .evidence_ops import SupersedeError, supersede
from .ingest.paste import import_pasted
from .models import Claim, ClaimType, Evidence, EvidenceKind, Opportunity, Product, Stage, SupplierQuote
from .profiles import available_profiles, load_profile

app = typer.Typer(help="宠物用品选品与经营决策平台（第 0 阶段：数据层 / 导入 / 成本引擎）", no_args_is_help=True)
opp_app = typer.Typer(help="管理研究机会", no_args_is_help=True)
app.add_typer(opp_app, name="opportunity")
ev_app = typer.Typer(help="证据操作", no_args_is_help=True)
app.add_typer(ev_app, name="evidence")
claim_app = typer.Typer(help="论断（带引用校验）", no_args_is_help=True)
app.add_typer(claim_app, name="claim")
db_app = typer.Typer(help="数据库迁移", no_args_is_help=True)
app.add_typer(db_app, name="db")
tool_app = typer.Typer(help="Agent 工具（调试用：查看定义、手动调用）", no_args_is_help=True)
app.add_typer(tool_app, name="tool")
run_app = typer.Typer(help="调查运行记录", no_args_is_help=True)
app.add_typer(run_app, name="run")
console = Console()

DbOpt = typer.Option(None, "--db", help="数据库文件，默认 private/data/petsourcing.db")


def _engine(db: Path | None):
    engine = make_engine(db)
    init_db(engine)
    return engine


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "opportunity"


def _get_opp(session, slug: str) -> Opportunity:
    opp = session.scalar(select(Opportunity).where(Opportunity.slug == slug))
    if opp is None:
        console.print(f"[red]找不到机会 '{slug}'，先运行 petsrc opportunity list 查看[/red]")
        raise typer.Exit(1)
    return opp


# --------------------------------------------------------------------------- init


@app.command()
def init(db: Path | None = DbOpt) -> None:
    """创建数据库（重复运行是安全的）。"""
    path = db or config.db_path()
    _engine(path)
    console.print(f"[green]数据库已就绪：[/green]{path}")


# --------------------------------------------------------------------------- opportunity


@opp_app.command("add")
def opp_add(
    title: str = typer.Option(..., help="机会标题"),
    slug: str | None = typer.Option(None, help="英文短名，默认由标题生成"),
    category: str = typer.Option("pet_supplies", help=f"品类配置：{available_profiles()}"),
    market: str = typer.Option("CA", help="目标市场"),
    description: str | None = typer.Option(None),
    db: Path | None = DbOpt,
) -> None:
    """新建一个研究机会。"""
    load_profile(category)  # fail fast on unknown profile
    slug = slug or _slugify(title)
    with session_scope(_engine(db)) as s:
        if s.scalar(select(Opportunity).where(Opportunity.slug == slug)):
            console.print(f"[yellow]'{slug}' 已存在，未重复创建[/yellow]")
            raise typer.Exit(0)
        s.add(Opportunity(slug=slug, title=title, category_profile=category,
                          target_market=market, description=description))
    console.print(f"[green]已创建：[/green]{slug}")


@opp_app.command("list")
def opp_list(db: Path | None = DbOpt) -> None:
    """列出所有机会及其证据数量。"""
    with session_scope(_engine(db)) as s:
        t = Table("slug", "标题", "阶段", "市场", "竞品", "证据", "报价")
        for o in s.scalars(select(Opportunity).order_by(Opportunity.id)):
            count = lambda m: s.scalar(select(func.count()).select_from(m).where(m.opportunity_id == o.id))  # noqa: E731
            t.add_row(o.slug, o.title, o.stage.value, o.target_market,
                      str(count(Product)), str(count(Evidence)), str(count(SupplierQuote)))
        console.print(t)


@opp_app.command("show")
def opp_show(slug: str, db: Path | None = DbOpt, today: str | None = typer.Option(None, help="YYYY-MM-DD，用于判断过期")) -> None:
    """查看一个机会的竞品与证据（含过期标记）。"""
    ref = date.fromisoformat(today) if today else date.today()
    with session_scope(_engine(db)) as s:
        o = _get_opp(s, slug)
        profile = load_profile(o.category_profile)
        console.print(f"[bold]{o.title}[/bold]  ({o.slug}, {profile.name}, {o.target_market}, {o.stage.value})")

        t = Table("key", "名称", "品牌", "价格", "评论数*", "评分", "采集日期")
        for p in o.products:
            price = f"{p.price:.2f} {p.currency}" if p.price is not None else "[dim]未知[/dim]"
            t.add_row(p.key, p.name, p.brand or "", price,
                      str(p.review_count) if p.review_count is not None else "未知",
                      str(p.rating) if p.rating is not None else "未知",
                      _date_cell(profile, "listing", p.captured_at, ref))
        console.print(t)
        console.print("[dim]* 评论数不等于销量[/dim]")

        t = Table("id", "类型", "商品", "内容", "来源", "采集日期")
        for e in o.evidence:
            if e.superseded_by_id:
                continue
            t.add_row(str(e.id), e.kind.value, e.product.key if e.product else "",
                      e.raw_text[:70] + ("…" if len(e.raw_text) > 70 else ""),
                      (e.source_url or e.source_note or "")[:40],
                      _date_cell(profile, e.kind.value, e.captured_at, ref))
        console.print(t)


def _date_cell(profile, kind: str, d: date, ref: date) -> str:
    return f"[red]{d} (可能过时)[/red]" if profile.is_stale(kind, d, ref) else str(d)


# --------------------------------------------------------------------------- import


@app.command("import")
def import_cmd(
    kind: str = typer.Argument(..., help="products | evidence | quotes"),
    file: Path = typer.Argument(..., exists=True, dir_okay=False),
    opportunity: str = typer.Option(..., "--opportunity", "-o", help="机会 slug"),
    strict: bool = typer.Option(False, help="任一行出错则整个文件不导入"),
    db: Path | None = DbOpt,
) -> None:
    """从 CSV 导入数据。重复导入同一文件不会产生重复记录。"""
    if kind not in IMPORTERS:
        console.print(f"[red]kind 必须是 {list(IMPORTERS)}[/red]")
        raise typer.Exit(2)
    with session_scope(_engine(db)) as s:
        report = IMPORTERS[kind](s, _get_opp(s, opportunity), file, strict=strict)
    _print_report(report)
    if report.aborted:
        raise typer.Exit(1)


def _print_report(r: ImportReport) -> None:
    status = "[red]已中止（strict）[/red]" if r.aborted else "[green]完成[/green]"
    console.print(f"{status} {r.kind}: 新增 {r.inserted}，重复跳过 {r.skipped_duplicates}，错误 {len(r.errors)}  (batch {r.batch})")
    for line, msg in r.errors:
        console.print(f"  [red]第 {line} 行[/red] {msg}")


# --------------------------------------------------------------------------- economics


@app.command()
def economics(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="情景 TOML 文件"),
    sens: float = typer.Option(0.2, "--sensitivity", help="敏感度分析的上下浮动比例"),
) -> None:
    """计算单件成本、资金需求和利润情景，并做敏感度分析。"""
    ss = load_scenarios(file)
    console.print(f"[bold]{ss.title}[/bold]  (CAD，不含销售税)")

    rows = [
        ("到岸成本/件", "landed_cost_per_unit", "{:.2f}"),
        ("单笔净收入", "net_per_sale", "{:.2f}"),
        ("单件贡献利润", "contribution_per_unit", "{:.2f}"),
        ("贡献利润率", "margin", "{:.1%}"),
        ("资金需求", "cash_needed", "{:,.0f}"),
        ("回本需卖出(件)", "units_to_recover", "{}"),
        ("预计卖出(件)", "units_sold", "{}"),
        ("本批利润", "profit", "{:,.0f}"),
        ("ROI", "roi", "{:.1%}"),
    ]
    t = Table("指标", *[sc.name for sc in ss.scenarios])
    for label, attr, fmt in rows:
        cells = []
        for sc in ss.scenarios:
            v = getattr(sc.result, attr)
            cells.append("永不回本" if v is None else fmt.format(v))
        t.add_row(label, *cells)
    console.print(t)

    for sc in ss.scenarios:
        for w in sc.result.warnings:
            console.print(f"[yellow]⚠ {escape(f'[{sc.name}]')} {w}[/yellow]")

    base = ss.scenarios[0]
    t = Table("参数", "值", "来源", "说明", title="base 情景参数来源")
    for name, src in base.sources.items():
        style = {"profile_default": "yellow", "assumption": "yellow"}.get(src.kind, "green")
        t.add_row(name, str(getattr(base.inputs, name)), f"[{style}]{src.kind}[/{style}]",
                  " ".join(x for x in (src.ref, src.note) if x))
    console.print(t)

    t = Table("参数", f"-{sens:.0%} 时利润", f"+{sens:.0%} 时利润", "波动",
              title="敏感度（哪个假设最要紧）")
    for x in sensitivity(base.inputs, sens):
        t.add_row(x.param, f"{x.low_profit:,.0f}", f"{x.high_profit:,.0f}", f"{x.swing:,.0f}")
    console.print(t)


# --------------------------------------------------------------------------- paste


@app.command()
def paste(
    opportunity: str = typer.Option(..., "--opportunity", "-o"),
    kind: EvidenceKind = typer.Option(EvidenceKind.REVIEW, help="证据类型"),
    source_url: str | None = typer.Option(None, help="来源链接"),
    source_note: str | None = typer.Option(None, help="没有链接时的来源说明"),
    captured_at: str | None = typer.Option(None, help="采集日期 YYYY-MM-DD，默认今天"),
    product: str | None = typer.Option(None, help="关联的竞品 key"),
    language: str | None = typer.Option(None),
    file: Path | None = typer.Option(None, exists=True, dir_okay=False, help="从文件读取，默认读标准输入"),
    db: Path | None = DbOpt,
) -> None:
    """粘贴多条文本作为证据：条目之间用空行或 --- 分隔。"""
    import sys

    text = file.read_text(encoding="utf-8") if file else sys.stdin.read()
    when = date.fromisoformat(captured_at) if captured_at else date.today()
    with session_scope(_engine(db)) as s:
        report = import_pasted(s, _get_opp(s, opportunity), text, kind=kind, captured_at=when,
                               source_url=source_url, source_note=source_note,
                               product_key=product, language=language)
    _print_report(report)


# --------------------------------------------------------------------------- evidence


@ev_app.command("supersede")
def ev_supersede(
    old_id: int = typer.Argument(..., help="被取代的旧证据 id"),
    new_id: int = typer.Argument(..., help="新证据 id"),
    note: str | None = typer.Option(None, help="原因，例如：价格已更新"),
    db: Path | None = DbOpt,
) -> None:
    """用新证据取代旧证据（旧证据保留为历史，但不再作为当前事实）。"""
    with session_scope(_engine(db)) as s:
        try:
            old = supersede(s, old_id, new_id, note)
        except SupersedeError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        affected = [cid for cid, evs in claims_citing_superseded(s, old.opportunity).items()
                    if old_id in evs]
    console.print(f"[green]证据 {old_id} 已被 {new_id} 取代[/green]")
    if affected:
        console.print(f"[yellow]以下论断引用了旧证据，需要复核：{affected}[/yellow]")


# --------------------------------------------------------------------------- claims


def _parse_cite(raw: str) -> Citation:
    eid, sep, quote = raw.partition(":")
    if not sep or not eid.strip().isdigit():
        raise typer.BadParameter(f"引用格式应为 证据id:原文片段，收到 {raw!r}")
    return Citation(int(eid), quote.strip())


@claim_app.command("add")
def claim_add(
    opportunity: str = typer.Option(..., "--opportunity", "-o"),
    type: ClaimType = typer.Option(..., "--type", help="pain / gap / cost / risk / other"),
    text: str = typer.Option(..., "--text", help="论断内容"),
    cite: list[str] = typer.Option([], "--cite", help="证据id:原文片段，可重复"),
    agent: str = typer.Option("manual", help="谁提出的"),
    confidence: float | None = typer.Option(None),
    db: Path | None = DbOpt,
) -> None:
    """添加论断。引用的原文必须真实存在于证据中，否则整条论断被拒绝。"""
    citations = [_parse_cite(c) for c in cite]
    with session_scope(_engine(db)) as s:
        r = add_claim(s, _get_opp(s, opportunity), agent=agent, type=type, text=text,
                      citations=citations, confidence=confidence)
    if not r.ok:
        console.print("[red]论断被拒绝：[/red]")
        for e in r.errors:
            console.print(f"  [red]✗[/red] {escape(e)}")
        raise typer.Exit(1)
    console.print(f"[green]已添加论断 #{r.claim_id}（{r.status.value}）[/green]")
    for w in r.warnings:
        console.print(f"  [yellow]⚠ {escape(w)}[/yellow]")


@claim_app.command("list")
def claim_list(opportunity: str = typer.Option(..., "--opportunity", "-o"), db: Path | None = DbOpt) -> None:
    """列出论断及其引用。"""
    with session_scope(_engine(db)) as s:
        opp = _get_opp(s, opportunity)
        stale = claims_citing_superseded(s, opp)
        t = Table("id", "类型", "状态", "来自", "论断", "引用")
        for c in s.scalars(select(Claim).where(Claim.opportunity_id == opp.id).order_by(Claim.id)):
            cites = "\n".join(f"#{x.evidence_id}: “{x.quote_span}”" for x in c.citations) or "[dim]无[/dim]"
            status = c.status.value
            if c.id in stale:
                status += f"\n[yellow]引用已被取代 {stale[c.id]}[/yellow]"
            t.add_row(str(c.id), c.type.value, status, c.agent, c.text, escape(cites) if c.citations else cites)
        console.print(t)


# --------------------------------------------------------------------------- db


@db_app.command("upgrade")
def db_upgrade(db: Path | None = DbOpt) -> None:
    """把数据库升级到最新结构（保留数据）。"""
    from .migrate import current_revision

    engine = _engine(db)
    console.print(f"[green]数据库版本：{current_revision(engine)}[/green]")


@db_app.command("revision")
def db_revision(message: str = typer.Option(..., "-m", help="迁移说明"), db: Path | None = DbOpt) -> None:
    """修改 models.py 后，自动生成新的迁移脚本（开发用）。"""
    from .migrate import revision

    revision(message, make_engine(db) if db else None)


# --------------------------------------------------------------------------- tools & runs


@tool_app.command("list")
def tool_list(agent: str = typer.Option("manual", help="只看某个 Agent 被授权的工具")) -> None:
    """列出 Agent 可用的工具。"""
    from .agents import AGENT_TOOLS, TOOLS

    if agent not in AGENT_TOOLS:
        console.print(f"[red]未知 Agent，可选：{sorted(AGENT_TOOLS)}[/red]")
        raise typer.Exit(2)
    t = Table("工具", "写入", "说明", "可用的 Agent", title=f"{agent} 可用的工具")
    for name in sorted(AGENT_TOOLS[agent]):
        tool = TOOLS[name]
        users = ", ".join(a for a in sorted(AGENT_TOOLS) if name in AGENT_TOOLS[a] and a != "manual")
        t.add_row(name, "✓" if tool.writes else "", tool.description, users)
    console.print(t)


@tool_app.command("call")
def tool_call(
    name: str = typer.Argument(..., help="工具名"),
    opportunity: str = typer.Option(..., "--opportunity", "-o"),
    args: str = typer.Option("{}", "--args", help='JSON 参数，例如 \'{"query": "clean"}\''),
    agent: str = typer.Option("manual", help="以哪个 Agent 的身份调用（会检查权限）"),
    run: int | None = typer.Option(None, help="追加到已有的运行记录，默认新建一条"),
    db: Path | None = DbOpt,
) -> None:
    """手动调用一个工具，结果和 Agent 调用时完全一样，并记录在运行日志中。"""
    import json

    from .agents import ToolExecutor, start_run
    from .models import ResearchRun

    try:
        parsed = json.loads(args)
    except json.JSONDecodeError as e:
        console.print(f"[red]--args 不是合法的 JSON：{e}[/red]")
        raise typer.Exit(2)
    with session_scope(_engine(db)) as s:
        opp = _get_opp(s, opportunity)
        r = s.get(ResearchRun, run) if run else start_run(s, opp, model="manual")
        if r is None or r.opportunity_id != opp.id:
            console.print(f"[red]运行记录 {run} 不存在或不属于这个机会[/red]")
            raise typer.Exit(1)
        result = ToolExecutor(s, opp, r, agent).call(name, parsed)
        run_id = r.id
    console.print_json(data=result)
    console.print(f"[dim]run {run_id}[/dim]")
    if not result["ok"]:
        raise typer.Exit(1)


@run_app.command("list")
def run_list(opportunity: str = typer.Option(..., "--opportunity", "-o"), db: Path | None = DbOpt) -> None:
    """列出一个机会的所有调查运行。"""
    from .models import AgentStep, ResearchRun

    with session_scope(_engine(db)) as s:
        opp = _get_opp(s, opportunity)
        t = Table("run", "状态", "模型", "提示词版本", "开始", "工具调用", "失败")
        for r in s.scalars(select(ResearchRun).where(ResearchRun.opportunity_id == opp.id).order_by(ResearchRun.id)):
            n = s.scalar(select(func.count()).select_from(AgentStep).where(AgentStep.run_id == r.id))
            bad = s.scalar(select(func.count()).select_from(AgentStep).where(AgentStep.run_id == r.id, AgentStep.ok.is_(False)))
            t.add_row(str(r.id), r.status, r.model or "", r.prompt_version or "",
                      r.started_at.strftime("%Y-%m-%d %H:%M"), str(n), f"[red]{bad}[/red]" if bad else "0")
        console.print(t)


@run_app.command("show")
def run_show(run_id: int, db: Path | None = DbOpt) -> None:
    """按顺序显示一次运行中每个 Agent 的每次工具调用（调查过程）。"""
    import json

    from .agents import run_steps
    from .models import ResearchRun

    with session_scope(_engine(db)) as s:
        r = s.get(ResearchRun, run_id)
        if r is None:
            console.print(f"[red]运行 {run_id} 不存在[/red]")
            raise typer.Exit(1)
        console.print(f"[bold]run {r.id}[/bold] · {r.status} · model={r.model} · prompt={r.prompt_version}"
                      + (f" · [red]{escape(r.error)}[/red]" if r.error else ""))
        t = Table("#", "Agent", "工具", "输入", "结果", "ms")
        for st in run_steps(s, run_id):
            res = "[green]ok[/green]" if st.ok else f"[red]{escape(st.error or '')}[/red]"
            t.add_row(str(st.id), st.agent, st.tool_name,
                      escape(json.dumps(st.tool_input, ensure_ascii=False)[:80]), res, str(st.latency_ms))
        console.print(t)


# --------------------------------------------------------------------------- demo


@app.command()
def demo(db: Path = typer.Option(config.DEMO_DB_PATH, "--db", help="演示数据库（会被重建）")) -> None:
    """用虚构示例数据重建一个演示数据库，适合录屏。"""
    if db.exists():
        db.unlink()
    engine = _engine(db)
    slug = "demo-easy-clean-cat-brush"
    with session_scope(engine) as s:
        opp = Opportunity(slug=slug, title="[DEMO] 易清洁猫咪梳毛工具", category_profile="pet_supplies",
                          target_market="CA", stage=Stage.RESEARCHING, is_demo=True,
                          description="虚构示例：验证『梳毛工具难清洁』是否是值得进入的痛点")
        s.add(opp)
        s.flush()
        for kind in ("products", "evidence", "quotes"):
            _print_report(IMPORTERS[kind](s, opp, config.SAMPLES_DIR / f"{kind}.csv"))
        _seed_demo_claims(s, opp)
    console.print(f"[green]演示数据库：[/green]{db}")
    console.print(f"下一步：petsrc opportunity show {slug} --db {db}")


def _seed_demo_claims(s, opp: Opportunity) -> None:
    """A few hand-written claims so the demo shows the citation rules at work."""

    def ev(fragment: str) -> int:
        return s.scalar(select(Evidence.id).where(Evidence.opportunity_id == opp.id,
                                                  Evidence.raw_text.contains(fragment)))

    add_claim(s, opp, agent="manual", type=ClaimType.PAIN,
              text="Users find grooming tools hard to clean because hair packs into the base or teeth.",
              citations=[Citation(ev("easy to clean"), "ends up with hair packed at the base"),
                         Citation(ev("stuck between the teeth"), "hair gets stuck between the teeth")])
    add_claim(s, opp, agent="manual", type=ClaimType.RISK,
              text="Some cats are scared of grooming tools, which limits who can use them.",
              citations=[Citation(ev("too sharp"), "my cat runs away as soon as she sees it"),
                         Citation(ev("water tank"), "the mist scares my cat")])
    add_claim(s, opp, agent="manual", type=ClaimType.GAP,
              text="No product on the local shelf advertises easy cleaning.")  # no citation -> unverified


if __name__ == "__main__":
    app()
