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
from .models import Evidence, Opportunity, Product, Stage, SupplierQuote
from .profiles import available_profiles, load_profile

app = typer.Typer(help="宠物用品选品与经营决策平台（第 0 阶段：数据层 / 导入 / 成本引擎）", no_args_is_help=True)
opp_app = typer.Typer(help="管理研究机会", no_args_is_help=True)
app.add_typer(opp_app, name="opportunity")
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
    console.print(f"[green]演示数据库：[/green]{db}")
    console.print(f"下一步：petsrc opportunity show {slug} --db {db}")


if __name__ == "__main__":
    app()
