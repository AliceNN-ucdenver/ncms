"""Authoritative software-development catalog.

Exhaustive surface-form → slot assignments for software-engineering
content.  Grounded where possible in external authority:

  - Wikidata (Q-numbers) via https://www.wikidata.org/
  - Wikipedia category membership
  - Stack Overflow tag wiki descriptions
  - GitHub Topics community taxonomy

Slot boundaries mirror
:data:`ncms.application.adapters.schemas.SLOT_TAXONOMY['software_dev']`:

  language   — programming language
  framework  — opinionated app / UI framework that dictates structure
  library    — imported dep that's NOT a framework
  database   — data store / cache / queue / search index / vector DB
  platform   — runtime / orchestration / cloud environment
  tool       — dev-time tooling (linters, editors, CI, test runners…)
  pattern    — architectural or coding pattern (concept, not a tool)
  alternative— (populated from others for contrast-partner role)
  frequency  — recurring time interval

Additions are welcomed — every entry should cite its authoritative
source so future maintainers can audit the assignment.  Prefer
Wikidata QIDs (they're stable); fall back to Wikipedia / SO tag
names when the QID isn't obvious.
"""

from __future__ import annotations

from ncms.application.adapters.sdg.catalog.primitives import CatalogEntry


# ── Languages (60+) ─────────────────────────────────────────────────

_LANGUAGES: tuple[CatalogEntry, ...] = (
    CatalogEntry("python",         "language", "language_runtime", ("python3", "cpython"), "wikidata:Q28865"),
    CatalogEntry("rust",           "language", "language_runtime", (), "wikidata:Q575650"),
    CatalogEntry("go",             "language", "language_runtime", ("golang",), "wikidata:Q37227"),
    CatalogEntry("typescript",     "language", "language_runtime", ("ts",), "wikidata:Q978185"),
    CatalogEntry("javascript",     "language", "language_runtime", ("js",), "wikidata:Q2005"),
    CatalogEntry("ruby",           "language", "language_runtime", (), "wikidata:Q161053"),
    CatalogEntry("java",           "language", "language_runtime", (), "wikidata:Q251"),
    CatalogEntry("kotlin",         "language", "language_runtime", (), "wikidata:Q15273616"),
    CatalogEntry("swift",          "language", "language_runtime", (), "wikidata:Q17118377"),
    CatalogEntry("c++",            "language", "language_runtime", ("cpp", "cplusplus"), "wikidata:Q2407"),
    CatalogEntry("c",              "language", "language_runtime", (), "wikidata:Q15777"),
    CatalogEntry("c#",             "language", "language_runtime", ("csharp",), "wikidata:Q2370"),
    CatalogEntry("elixir",         "language", "language_runtime", (), "wikidata:Q13366104"),
    CatalogEntry("scala",          "language", "language_runtime", (), "wikidata:Q460584"),
    CatalogEntry("clojure",        "language", "language_runtime", (), "wikidata:Q29132"),
    CatalogEntry("haskell",        "language", "language_runtime", (), "wikidata:Q35571"),
    CatalogEntry("ocaml",          "language", "language_runtime", (), "wikidata:Q261395"),
    CatalogEntry("f#",             "language", "language_runtime", ("fsharp",), "wikidata:Q1341478"),
    CatalogEntry("php",            "language", "language_runtime", (), "wikidata:Q59"),
    CatalogEntry("perl",           "language", "language_runtime", (), "wikidata:Q42478"),
    CatalogEntry("lua",            "language", "language_runtime", (), "wikidata:Q207316"),
    CatalogEntry("zig",            "language", "language_runtime", (), "wikidata:Q65063361"),
    CatalogEntry("nim",            "language", "language_runtime", (), "wikidata:Q15064735"),
    CatalogEntry("dart",           "language", "language_runtime", (), "wikidata:Q406009"),
    CatalogEntry("julia",          "language", "language_runtime", (), "wikidata:Q15608983"),
    CatalogEntry("r",              "language", "language_runtime", ("r language",), "wikidata:Q206904"),
    CatalogEntry("matlab",         "language", "language_runtime", (), "wikidata:Q169759"),
    CatalogEntry("bash",           "language", "language_runtime", (), "wikidata:Q189248"),
    CatalogEntry("shell",          "language", "language_runtime", ("sh",), "SO-tag:shell"),
    CatalogEntry("powershell",     "language", "language_runtime", (), "wikidata:Q840410"),
    CatalogEntry("sql",            "language", "language_runtime", (), "wikidata:Q47607"),
    CatalogEntry("html",           "language", "language_runtime", ("html5",), "wikidata:Q8811"),
    CatalogEntry("css",            "language", "language_runtime", ("css3",), "wikidata:Q46441"),
    CatalogEntry("sass",           "language", "language_runtime", ("scss",), "wikidata:Q1572865"),
    CatalogEntry("less",           "language", "language_runtime", (), "wikidata:Q1807893"),
    CatalogEntry("assembly",       "language", "language_runtime", ("asm",), "wikidata:Q165436"),
    CatalogEntry("cobol",          "language", "language_runtime", (), "wikidata:Q131991"),
    CatalogEntry("fortran",        "language", "language_runtime", (), "wikidata:Q83303"),
    CatalogEntry("pascal",         "language", "language_runtime", (), "wikidata:Q81571"),
    CatalogEntry("ada",            "language", "language_runtime", (), "wikidata:Q154755"),
    CatalogEntry("erlang",         "language", "language_runtime", (), "wikidata:Q334749"),
    CatalogEntry("crystal",        "language", "language_runtime", (), "wikidata:Q17045700"),
    CatalogEntry("groovy",         "language", "language_runtime", ("apache groovy",), "wikidata:Q674970"),
    CatalogEntry("visual basic",   "language", "language_runtime", ("vb", "vb.net"), "wikidata:Q73562"),
    CatalogEntry("smalltalk",      "language", "language_runtime", (), "wikidata:Q189855"),
    CatalogEntry("prolog",         "language", "language_runtime", (), "wikidata:Q163468"),
    CatalogEntry("lisp",           "language", "language_runtime", (), "wikidata:Q132874"),
    CatalogEntry("common lisp",    "language", "language_runtime", ("cl",), "wikidata:Q193321"),
    CatalogEntry("racket",         "language", "language_runtime", (), "wikidata:Q336432"),
    CatalogEntry("scheme",         "language", "language_runtime", (), "wikidata:Q18376"),
    CatalogEntry("tcl",            "language", "language_runtime", (), "wikidata:Q287474"),
    CatalogEntry("solidity",       "language", "language_runtime", (), "wikidata:Q47460408"),
    CatalogEntry("elm",            "language", "language_runtime", (), "wikidata:Q22670101"),
    CatalogEntry("reason",         "language", "language_runtime", ("reasonml",), "wikipedia:ReasonML"),
    CatalogEntry("rescript",       "language", "language_runtime", (), "github-topic:rescript"),
    CatalogEntry("gleam",          "language", "language_runtime", (), "wikidata:Q126086866"),
)


# ── Frameworks (120+) ───────────────────────────────────────────────
# "opinionated app / UI framework that dictates structure"

_FRAMEWORKS: tuple[CatalogEntry, ...] = (
    # Python web
    CatalogEntry("django",         "framework", "framework", (), "wikidata:Q213907"),
    CatalogEntry("fastapi",        "framework", "framework", (), "wikidata:Q105187478"),
    CatalogEntry("flask",          "framework", "framework", (), "wikidata:Q16924931"),
    CatalogEntry("pyramid",        "framework", "framework", (), "wikidata:Q4067528"),
    CatalogEntry("starlette",      "framework", "framework", (), "github-topic:starlette"),
    CatalogEntry("tornado",        "framework", "framework", (), "wikidata:Q3818593"),
    CatalogEntry("sanic",          "framework", "framework", (), "github-topic:sanic"),
    # Ruby
    CatalogEntry("rails",          "framework", "framework", ("ruby on rails", "ror"), "wikidata:Q189621"),
    CatalogEntry("sinatra",        "framework", "framework", (), "wikidata:Q3305564"),
    CatalogEntry("hanami",         "framework", "framework", (), "github-topic:hanami"),
    # Node / JS back-end
    CatalogEntry("express",        "framework", "framework", ("expressjs", "express.js"), "wikidata:Q16876055"),
    CatalogEntry("koa",            "framework", "framework", ("koajs",), "github-topic:koa"),
    CatalogEntry("nestjs",         "framework", "framework", ("nest.js", "nest",), "github-topic:nestjs"),
    CatalogEntry("fastify",        "framework", "framework", (), "github-topic:fastify"),
    CatalogEntry("hapi",           "framework", "framework", (), "github-topic:hapi"),
    CatalogEntry("adonisjs",       "framework", "framework", ("adonis",), "github-topic:adonisjs"),
    # Java / Kotlin / Scala
    CatalogEntry("spring boot",    "framework", "framework", ("spring",), "wikidata:Q22312671"),
    CatalogEntry("micronaut",      "framework", "framework", (), "wikidata:Q54848828"),
    CatalogEntry("quarkus",        "framework", "framework", (), "wikidata:Q62043672"),
    CatalogEntry("ktor",           "framework", "framework", (), "github-topic:ktor"),
    CatalogEntry("play",           "framework", "framework", ("play framework",), "wikidata:Q7205599"),
    CatalogEntry("vert.x",         "framework", "framework", ("vertx",), "github-topic:vertx"),
    CatalogEntry("dropwizard",     "framework", "framework", (), "github-topic:dropwizard"),
    # Go
    CatalogEntry("gin",            "framework", "framework", ("gin-gonic",), "github-topic:gin-gonic"),
    CatalogEntry("echo",           "framework", "framework", ("labstack/echo",), "github-topic:echo"),
    CatalogEntry("fiber",          "framework", "framework", ("gofiber",), "github-topic:gofiber"),
    CatalogEntry("chi",            "framework", "framework", (), "github-topic:chi"),
    CatalogEntry("beego",          "framework", "framework", (), "github-topic:beego"),
    # Rust
    CatalogEntry("actix",          "framework", "framework", ("actix-web",), "github-topic:actix-web"),
    CatalogEntry("rocket",         "framework", "framework", (), "github-topic:rocket-rs"),
    CatalogEntry("axum",           "framework", "framework", (), "github-topic:axum"),
    CatalogEntry("warp",           "framework", "framework", (), "github-topic:warp-rs"),
    CatalogEntry("tower",          "framework", "framework", (), "github-topic:tower-rs"),
    # PHP
    CatalogEntry("laravel",        "framework", "framework", (), "wikidata:Q5891589"),
    CatalogEntry("symfony",        "framework", "framework", (), "wikidata:Q735997"),
    CatalogEntry("codeigniter",    "framework", "framework", (), "wikidata:Q1276830"),
    # .NET
    CatalogEntry("asp.net",        "framework", "framework", ("aspnet", "asp.net core"), "wikidata:Q2220433"),
    CatalogEntry(".net",           "framework", "framework", ("dotnet", ".net core"), "wikidata:Q5276"),
    # Elixir / Erlang
    CatalogEntry("phoenix",        "framework", "framework", (), "wikidata:Q16995380"),
    # Python data / ml
    CatalogEntry("pytorch",        "framework", "framework", (), "wikidata:Q47509047"),
    CatalogEntry("tensorflow",     "framework", "framework", (), "wikidata:Q21010103"),
    CatalogEntry("jax",            "framework", "framework", (), "github-topic:jax"),
    CatalogEntry("keras",          "framework", "framework", (), "wikidata:Q24981894"),
    CatalogEntry("langchain",      "framework", "framework", (), "github-topic:langchain"),
    CatalogEntry("llamaindex",     "framework", "framework", ("llama-index",), "github-topic:llamaindex"),
    CatalogEntry("huggingface",    "framework", "framework", ("hugging face", "hf",), "wikidata:Q108943604"),

    # JS / TS front-end app frameworks
    CatalogEntry("react",          "framework", "framework", ("reactjs",), "wikidata:Q19799061"),
    CatalogEntry("vue",            "framework", "framework", ("vue.js", "vuejs"), "wikidata:Q21028111"),
    CatalogEntry("svelte",         "framework", "framework", (), "wikidata:Q55065267"),
    CatalogEntry("sveltekit",      "framework", "framework", (), "github-topic:sveltekit"),
    CatalogEntry("angular",        "framework", "framework", ("angularjs", "angular2"), "wikidata:Q19707865"),
    CatalogEntry("next.js",        "framework", "framework", ("nextjs",), "wikidata:Q87811196"),
    CatalogEntry("nuxt",           "framework", "framework", ("nuxt.js", "nuxtjs"), "wikidata:Q55235213"),
    CatalogEntry("remix",          "framework", "framework", ("remix-run",), "github-topic:remix-run"),
    CatalogEntry("solid",          "framework", "framework", ("solidjs", "solid.js"), "github-topic:solidjs"),
    CatalogEntry("astro",          "framework", "framework", (), "github-topic:astro"),
    CatalogEntry("qwik",           "framework", "framework", (), "github-topic:qwik"),
    CatalogEntry("gatsby",         "framework", "framework", ("gatsbyjs",), "wikidata:Q63966451"),
    CatalogEntry("ember",          "framework", "framework", ("ember.js", "emberjs"), "wikidata:Q6887833"),
    CatalogEntry("backbone",       "framework", "framework", ("backbone.js",), "wikidata:Q4837909"),
    CatalogEntry("preact",         "framework", "framework", (), "github-topic:preact"),
    CatalogEntry("lit",            "framework", "framework", ("lit-html", "lit-element"), "github-topic:lit"),
    CatalogEntry("alpine.js",      "framework", "framework", ("alpinejs", "alpine",), "github-topic:alpinejs"),
    CatalogEntry("stimulus",       "framework", "framework", ("stimulusjs",), "github-topic:stimulus"),
    CatalogEntry("htmx",           "framework", "framework", (), "github-topic:htmx"),
    CatalogEntry("meteor",         "framework", "framework", ("meteor.js",), "wikidata:Q6817482"),

    # CSS frameworks (opinionated layout / design systems)
    CatalogEntry("bulma",          "framework", "framework", (), "github-topic:bulma"),
    CatalogEntry("bootstrap",      "framework", "framework", ("bootstrap css",), "wikidata:Q5302358"),
    CatalogEntry("foundation",     "framework", "framework", ("zurb foundation",), "wikidata:Q4154943"),
    CatalogEntry("pure.css",       "framework", "framework", ("purecss",), "github-topic:purecss"),
    CatalogEntry("semantic ui",    "framework", "framework", ("semanticui", "semantic"), "github-topic:semantic-ui"),

    # Mobile / cross-platform app frameworks
    CatalogEntry("flutter",        "framework", "framework", (), "wikidata:Q41819"),
    CatalogEntry("react native",   "framework", "framework", ("react-native",), "wikidata:Q24982615"),
    CatalogEntry("ionic",          "framework", "framework", (), "wikidata:Q18344221"),
    CatalogEntry("nativescript",   "framework", "framework", (), "wikidata:Q22959245"),
    CatalogEntry("xamarin",        "framework", "framework", (), "wikidata:Q1969217"),
    CatalogEntry("maui",           "framework", "framework", (".net maui",), "wikidata:Q107460001"),
    CatalogEntry("swiftui",        "framework", "framework", (), "wikidata:Q65085467"),
    CatalogEntry("uikit",          "framework", "framework", (), "wikidata:Q2462601"),
    CatalogEntry("jetpack compose","framework", "framework", ("compose",), "github-topic:jetpack-compose"),

    # Game engines (app frameworks for games)
    CatalogEntry("unity",          "framework", "framework", (), "wikidata:Q63241"),
    CatalogEntry("unreal engine",  "framework", "framework", ("unreal",), "wikidata:Q193866"),
    CatalogEntry("godot",          "framework", "framework", (), "wikidata:Q16964444"),
    CatalogEntry("bevy",           "framework", "framework", (), "github-topic:bevy"),
    CatalogEntry("phaser",         "framework", "framework", (), "github-topic:phaserjs"),

    # ── v9 coverage-audit backfill ──
    # Modern meta-frameworks (gaps in existing catalog)
    CatalogEntry("astro",          "framework", "framework", (), "wikidata:Q108980884"),
    CatalogEntry("solid",          "framework", "framework", ("solidjs",), "github-topic:solidjs"),
    CatalogEntry("qwik",           "framework", "framework", (), "github-topic:qwik"),
    CatalogEntry("hono",           "framework", "framework", (), "github-topic:hono"),
    CatalogEntry("encore",         "framework", "framework", (), "github-topic:encore"),
    CatalogEntry("htmx",           "framework", "framework", (), "wikidata:Q124003149"),
    CatalogEntry("alpinejs",       "framework", "framework", ("alpine.js",), "github-topic:alpinejs"),
    CatalogEntry("lit",            "framework", "framework", ("lit-element", "lit-html"), "github-topic:lit"),
    CatalogEntry("stencil",        "framework", "framework", ("stenciljs",), "github-topic:stenciljs"),
    # AI / ML frameworks (heavily referenced in modern ADRs)
    CatalogEntry("langchain",      "framework", "framework", (), "wikidata:Q115949144"),
    CatalogEntry("llamaindex",     "framework", "framework", ("llama index",), "github-topic:llamaindex"),
    CatalogEntry("haystack",       "framework", "framework", (), "github-topic:haystack"),
    CatalogEntry("dspy",           "framework", "framework", (), "github-topic:dspy"),
)


# ── Libraries (130+) ────────────────────────────────────────────────
# "imported code dep that's NOT a framework"

_LIBRARIES: tuple[CatalogEntry, ...] = (
    # Python data/validation/orm
    CatalogEntry("pydantic",       "library", "framework", (), "github-topic:pydantic"),
    CatalogEntry("zod",            "library", "framework", (), "github-topic:zod"),
    CatalogEntry("marshmallow",    "library", "framework", (), "github-topic:marshmallow"),
    CatalogEntry("sqlalchemy",     "library", "framework", (), "wikidata:Q7569209"),
    CatalogEntry("alembic",        "library", "framework", (), "github-topic:alembic"),
    CatalogEntry("activerecord",   "library", "framework", ("active record",), "wikidata:Q4680128"),
    CatalogEntry("prisma",         "library", "framework", (), "github-topic:prisma"),
    CatalogEntry("typeorm",        "library", "framework", (), "github-topic:typeorm"),
    # Database-tier middleware (connection poolers, HA managers).
    CatalogEntry("patroni",        "library", "infra", (), "github-topic:patroni"),
    CatalogEntry("pgbouncer",      "library", "infra", ("pg-bouncer", "pg_bouncer"), "github-topic:pgbouncer"),
    CatalogEntry("pgpool",         "library", "infra", ("pgpool-ii",), "github-topic:pgpool"),
    CatalogEntry("ecto",           "library", "framework", (), "github-topic:ecto"),
    CatalogEntry("gorm",           "library", "framework", (), "github-topic:gorm"),
    CatalogEntry("diesel",         "library", "framework", ("diesel-rs",), "github-topic:diesel"),
    CatalogEntry("sqlx",           "library", "framework", (), "github-topic:sqlx"),
    CatalogEntry("mongoose",       "library", "framework", (), "github-topic:mongoose"),
    CatalogEntry("sequelize",      "library", "framework", (), "github-topic:sequelize"),

    # HTTP clients
    CatalogEntry("requests",       "library", "framework", ("python requests",), "wikidata:Q17024469"),
    CatalogEntry("httpx",          "library", "framework", (), "github-topic:httpx"),
    CatalogEntry("aiohttp",        "library", "framework", (), "github-topic:aiohttp"),
    CatalogEntry("urllib3",        "library", "framework", (), "github-topic:urllib3"),
    CatalogEntry("axios",          "library", "framework", (), "wikidata:Q28131382"),
    CatalogEntry("got",            "library", "framework", (), "github-topic:got"),
    CatalogEntry("ky",             "library", "framework", (), "github-topic:ky"),
    CatalogEntry("node-fetch",     "library", "framework", (), "github-topic:node-fetch"),

    # Utility / functional JS
    CatalogEntry("lodash",         "library", "framework", (), "wikidata:Q20101840"),
    CatalogEntry("ramda",          "library", "framework", (), "github-topic:ramdajs"),
    CatalogEntry("date-fns",       "library", "framework", (), "github-topic:date-fns"),
    CatalogEntry("moment.js",      "library", "framework", ("moment", "momentjs"), "wikidata:Q60714120"),
    CatalogEntry("dayjs",          "library", "framework", (), "github-topic:dayjs"),
    CatalogEntry("luxon",          "library", "framework", (), "github-topic:luxon"),
    CatalogEntry("immer",          "library", "framework", (), "github-topic:immer"),
    CatalogEntry("immutable.js",   "library", "framework", ("immutablejs",), "github-topic:immutable-js"),

    # State management
    CatalogEntry("redux",          "library", "framework", (), "wikidata:Q22116717"),
    CatalogEntry("mobx",           "library", "framework", (), "github-topic:mobx"),
    CatalogEntry("zustand",        "library", "framework", (), "github-topic:zustand"),
    CatalogEntry("jotai",          "library", "framework", (), "github-topic:jotai"),
    CatalogEntry("recoil",         "library", "framework", (), "github-topic:recoiljs"),
    CatalogEntry("pinia",          "library", "framework", (), "github-topic:pinia"),
    CatalogEntry("vuex",           "library", "framework", (), "github-topic:vuex"),
    CatalogEntry("rxjs",           "library", "framework", (), "github-topic:rxjs"),

    # Background-work / queues (client libs)
    CatalogEntry("celery",         "library", "framework", (), "wikidata:Q15977484"),
    CatalogEntry("rq",             "library", "framework", (), "github-topic:python-rq"),
    CatalogEntry("sidekiq",        "library", "framework", (), "github-topic:sidekiq"),
    CatalogEntry("bullmq",         "library", "framework", (), "github-topic:bullmq"),
    CatalogEntry("bee-queue",      "library", "framework", (), "github-topic:bee-queue"),

    # Scientific / ML data libs (libraries, not frameworks)
    CatalogEntry("numpy",          "library", "framework", (), "wikidata:Q2011"),
    CatalogEntry("pandas",         "library", "framework", (), "wikidata:Q57817"),
    CatalogEntry("scipy",          "library", "framework", (), "wikidata:Q726145"),
    CatalogEntry("scikit-learn",   "library", "framework", ("sklearn",), "wikidata:Q1026367"),
    CatalogEntry("xgboost",        "library", "framework", (), "github-topic:xgboost"),
    CatalogEntry("lightgbm",       "library", "framework", (), "github-topic:lightgbm"),
    CatalogEntry("catboost",       "library", "framework", (), "github-topic:catboost"),
    CatalogEntry("matplotlib",     "library", "framework", (), "wikidata:Q881419"),
    CatalogEntry("seaborn",        "library", "framework", (), "github-topic:seaborn"),
    CatalogEntry("plotly",         "library", "framework", (), "wikidata:Q25050258"),
    CatalogEntry("bokeh",          "library", "framework", (), "github-topic:bokeh"),
    CatalogEntry("altair",         "library", "framework", (), "github-topic:altair"),
    CatalogEntry("pillow",         "library", "framework", ("pil",), "github-topic:pillow"),
    CatalogEntry("opencv",         "library", "framework", (), "wikidata:Q192234"),
    CatalogEntry("transformers",   "library", "framework", (), "github-topic:transformers"),
    CatalogEntry("sentence-transformers", "library", "framework", (), "github-topic:sentence-transformers"),
    CatalogEntry("spacy",          "library", "framework", (), "wikidata:Q22939042"),
    CatalogEntry("nltk",           "library", "framework", (), "wikidata:Q16039380"),
    CatalogEntry("gensim",         "library", "framework", (), "github-topic:gensim"),

    # HTML/parsing / scraping
    CatalogEntry("beautifulsoup",  "library", "framework", ("bs4",), "wikidata:Q4880080"),
    CatalogEntry("lxml",           "library", "framework", (), "wikidata:Q1820468"),
    CatalogEntry("cheerio",        "library", "framework", (), "github-topic:cheerio"),

    # CSS utility libs + component libs (library vs framework rule:
    # frameworks are CSS-only opinionated; component libs are JS
    # component sets that PLUG INTO a framework)
    CatalogEntry("tailwind",       "library", "framework", ("tailwindcss", "tailwind css"), "wikidata:Q77184034"),
    CatalogEntry("chakra ui",      "library", "framework", ("chakra",), "github-topic:chakra-ui"),
    CatalogEntry("material ui",    "library", "framework", ("mui",), "github-topic:material-ui"),
    CatalogEntry("shadcn/ui",      "library", "framework", ("shadcn",), "github-topic:shadcn-ui"),
    CatalogEntry("ant design",     "library", "framework", ("antd",), "github-topic:ant-design"),
    CatalogEntry("headless ui",    "library", "framework", (), "github-topic:headlessui"),
    CatalogEntry("radix ui",       "library", "framework", ("radix",), "github-topic:radix-ui"),
    CatalogEntry("styled-components","library","framework", (), "github-topic:styled-components"),
    CatalogEntry("emotion",        "library", "framework", ("emotion js",), "github-topic:emotion"),

    # Testing assertion / mock libs (libraries, not test runners)
    CatalogEntry("chai",           "library", "framework", (), "github-topic:chai"),
    CatalogEntry("sinon",          "library", "framework", (), "github-topic:sinon"),
    CatalogEntry("supertest",      "library", "framework", (), "github-topic:supertest"),
    CatalogEntry("msw",            "library", "framework", ("mock service worker",), "github-topic:msw"),
    CatalogEntry("faker",          "library", "framework", ("faker.js",), "github-topic:faker"),
    CatalogEntry("factory_boy",    "library", "framework", ("factory-boy",), "github-topic:factory-boy"),
    CatalogEntry("hypothesis",     "library", "framework", (), "github-topic:hypothesis"),

    # Auth / security client libs
    CatalogEntry("passport.js",    "library", "framework", ("passport", "passportjs"), "github-topic:passport"),
    CatalogEntry("devise",         "library", "framework", (), "github-topic:devise"),
    CatalogEntry("authlib",        "library", "framework", (), "github-topic:authlib"),

    # Graphql clients
    CatalogEntry("apollo",         "library", "framework", ("apollo client",), "github-topic:apollo-client"),
    CatalogEntry("graphql-yoga",   "library", "framework", (), "github-topic:graphql-yoga"),
    CatalogEntry("relay",          "library", "framework", ("relay.js",), "github-topic:relay"),

    # Component libs
    CatalogEntry("svar",           "library", "framework", (), "github-topic:svar"),  # observed in MSEB

    # ── v9 coverage-audit backfill (from MSEB software_dev benchmarks) ──
    CatalogEntry("jquery",         "library", "framework", ("jquery ui",), "wikidata:Q76736"),
    CatalogEntry("grpc",           "library", "framework", ("grpc-web", "grpc-gateway"), "wikidata:Q27134141"),
    CatalogEntry("d3.js",          "library", "framework", ("d3", "d3js"), "wikidata:Q1135511"),
    CatalogEntry("chart.js",       "library", "framework", ("chartjs",), "wikidata:Q85692663"),
    CatalogEntry("echarts",        "library", "framework", ("apache echarts",), "wikidata:Q26842593"),
    CatalogEntry("highcharts",     "library", "framework", (), "wikidata:Q1619840"),
    CatalogEntry("plotly",         "library", "framework", ("plotly.js", "plotly.py"), "wikidata:Q28045407"),
    CatalogEntry("protocol buffers", "library", "framework", ("protobuf", "protobufs"), "wikidata:Q3066430"),
    CatalogEntry("apache thrift",  "library", "framework", ("thrift",), "wikidata:Q1091443"),
)


# ── Databases / caches / queues / search (70+) ──────────────────────
# "stores, queries, queues, or indexes data structurally"

_DATABASES: tuple[CatalogEntry, ...] = (
    # Relational
    CatalogEntry("postgres",       "database", "infra", ("postgresql", "postgres sql", "pg"), "wikidata:Q192490"),
    CatalogEntry("mysql",          "database", "infra", (), "wikidata:Q850"),
    CatalogEntry("mariadb",        "database", "infra", (), "wikidata:Q787177"),
    CatalogEntry("sqlite",         "database", "infra", (), "wikidata:Q319417"),
    CatalogEntry("cockroachdb",    "database", "infra", ("cockroach",), "wikidata:Q29953626"),
    CatalogEntry("yugabytedb",     "database", "infra", ("yugabyte", "yugabyte db"), "wikidata:Q108137987"),
    CatalogEntry("tidb",           "database", "infra", ("ti-db",), "wikidata:Q66339263"),
    CatalogEntry("spanner",        "database", "infra", ("google spanner", "cloud spanner"), "wikidata:Q21063836"),
    CatalogEntry("aurora",         "database", "infra", ("amazon aurora", "aws aurora"), "wikidata:Q19575725"),
    CatalogEntry("planetscale",    "database", "infra", (), "github-topic:planetscale"),
    CatalogEntry("singlestore",    "database", "infra", ("memsql",), "wikidata:Q22337128"),
    CatalogEntry("percona",        "database", "infra", ("percona server",), "wikidata:Q7169108"),
    # (patroni / pgbouncer / pgpool are listed in the _LIBRARIES
    # tuple below — they're database-tier middleware, not databases
    # themselves.  Keeping them out of _DATABASES prevents the SDG
    # database pool from drawing them as primary values.)
    CatalogEntry("sql server",     "database", "infra", ("mssql", "microsoft sql server"), "wikidata:Q215819"),
    CatalogEntry("oracle",         "database", "infra", ("oracle database", "oracle db"), "wikidata:Q44940"),
    CatalogEntry("db2",            "database", "infra", ("ibm db2",), "wikidata:Q232386"),
    CatalogEntry("sybase",         "database", "infra", (), "wikidata:Q1342523"),
    CatalogEntry("firebird",       "database", "infra", ("firebird sql",), "wikidata:Q685289"),
    CatalogEntry("h2",             "database", "infra", ("h2 database",), "wikidata:Q5629464"),

    # Document / columnar / wide
    CatalogEntry("mongodb",        "database", "infra", ("mongo",), "wikidata:Q1166233"),
    CatalogEntry("couchdb",        "database", "infra", ("apache couchdb",), "wikidata:Q304529"),
    CatalogEntry("couchbase",      "database", "infra", (), "wikidata:Q5175314"),
    CatalogEntry("rethinkdb",      "database", "infra", (), "wikidata:Q3511786"),
    CatalogEntry("arangodb",       "database", "infra", (), "wikidata:Q15275212"),
    CatalogEntry("firestore",      "database", "infra", ("google firestore",), "wikidata:Q46748"),
    CatalogEntry("cosmos db",      "database", "infra", ("cosmosdb", "azure cosmos db"), "wikidata:Q28801820"),
    CatalogEntry("dynamodb",       "database", "infra", ("amazon dynamodb",), "wikidata:Q7853252"),
    CatalogEntry("cassandra",      "database", "infra", ("apache cassandra",), "wikidata:Q487897"),
    CatalogEntry("scylladb",       "database", "infra", ("scylla",), "wikidata:Q20712389"),
    CatalogEntry("hbase",          "database", "infra", ("apache hbase",), "wikidata:Q1125816"),
    CatalogEntry("bigtable",       "database", "infra", ("google bigtable",), "wikidata:Q1130036"),
    CatalogEntry("clickhouse",     "database", "infra", (), "wikidata:Q54866192"),
    CatalogEntry("apache druid",   "database", "infra", ("druid",), "wikidata:Q24082867"),
    CatalogEntry("pinot",          "database", "infra", ("apache pinot",), "github-topic:apache-pinot"),

    # Caches / kv
    CatalogEntry("redis",          "database", "infra", (), "wikidata:Q2136322"),
    CatalogEntry("memcached",      "database", "infra", (), "wikidata:Q131594"),
    CatalogEntry("etcd",           "database", "infra", (), "wikidata:Q22062404"),
    CatalogEntry("consul",         "database", "infra", ("hashicorp consul",), "wikidata:Q28966779"),
    CatalogEntry("riak",           "database", "infra", (), "wikidata:Q2704381"),
    CatalogEntry("hazelcast",      "database", "infra", (), "wikidata:Q15080389"),
    CatalogEntry("ignite",         "database", "infra", ("apache ignite",), "wikidata:Q23019902"),

    # Message queues / streams
    CatalogEntry("kafka",          "database", "infra", ("apache kafka",), "wikidata:Q16172024"),
    CatalogEntry("rabbitmq",       "database", "infra", (), "wikidata:Q1037826"),
    CatalogEntry("nats",           "database", "infra", ("nats.io",), "github-topic:nats-io"),
    CatalogEntry("sqs",            "database", "infra", ("amazon sqs",), "wikidata:Q4744893"),
    CatalogEntry("pulsar",         "database", "infra", ("apache pulsar",), "wikidata:Q54949906"),
    CatalogEntry("activemq",       "database", "infra", ("apache activemq",), "wikidata:Q624253"),
    CatalogEntry("zeromq",         "database", "infra", ("0mq",), "wikidata:Q1193817"),
    CatalogEntry("google pub/sub", "database", "infra", ("google cloud pub/sub",), "wikidata:Q2001552"),
    CatalogEntry("eventbridge",    "database", "infra", ("aws eventbridge",), "github-topic:aws-eventbridge"),
    CatalogEntry("kinesis",        "database", "infra", ("amazon kinesis",), "wikidata:Q17013267"),

    # Graph
    CatalogEntry("neo4j",          "database", "infra", (), "wikidata:Q1628290"),
    CatalogEntry("janusgraph",     "database", "infra", (), "wikidata:Q30633878"),
    CatalogEntry("dgraph",         "database", "infra", (), "github-topic:dgraph"),
    CatalogEntry("tigergraph",     "database", "infra", (), "github-topic:tigergraph"),
    CatalogEntry("neptune",        "database", "infra", ("amazon neptune",), "wikidata:Q43024611"),

    # Search
    CatalogEntry("elasticsearch",  "database", "infra", (), "wikidata:Q3050461"),
    CatalogEntry("opensearch",     "database", "infra", (), "wikidata:Q94313917"),
    CatalogEntry("solr",           "database", "infra", ("apache solr",), "wikidata:Q2367919"),
    CatalogEntry("meilisearch",    "database", "infra", (), "github-topic:meilisearch"),
    CatalogEntry("typesense",      "database", "infra", (), "github-topic:typesense"),
    CatalogEntry("algolia",        "database", "infra", (), "wikidata:Q28006024"),
    CatalogEntry("sphinx search",  "database", "infra", ("sphinx",), "wikidata:Q1462098"),

    # Time-series
    CatalogEntry("influxdb",       "database", "infra", (), "wikidata:Q21998975"),
    CatalogEntry("timescaledb",    "database", "infra", (), "wikidata:Q66327077"),
    CatalogEntry("prometheus",     "database", "infra", ("prometheus db", "prometheus tsdb"), "wikidata:Q56467477"),
    CatalogEntry("questdb",        "database", "infra", (), "github-topic:questdb"),
    CatalogEntry("victoriametrics","database", "infra", (), "github-topic:victoriametrics"),

    # Vector DBs
    CatalogEntry("pinecone",       "database", "infra", (), "github-topic:pinecone"),
    CatalogEntry("weaviate",       "database", "infra", (), "github-topic:weaviate"),
    CatalogEntry("qdrant",         "database", "infra", (), "github-topic:qdrant"),
    CatalogEntry("milvus",         "database", "infra", (), "github-topic:milvus"),
    CatalogEntry("chroma",         "database", "infra", ("chromadb",), "github-topic:chroma"),
    CatalogEntry("pgvector",       "database", "infra", (), "github-topic:pgvector"),
    CatalogEntry("lancedb",        "database", "infra", (), "github-topic:lancedb"),

    # Analytics / warehouse
    CatalogEntry("snowflake",      "database", "infra", (), "wikidata:Q28055441"),
    CatalogEntry("bigquery",       "database", "infra", ("google bigquery",), "wikidata:Q17068048"),
    CatalogEntry("redshift",       "database", "infra", ("amazon redshift",), "wikidata:Q17068048"),
    CatalogEntry("databricks",     "database", "infra", (), "wikidata:Q24945013"),
    CatalogEntry("duckdb",         "database", "infra", (), "github-topic:duckdb"),

    # ── v9 coverage-audit backfill (MSEB + modern additions) ──
    CatalogEntry("clickhouse",     "database", "infra", (), "wikidata:Q55568627"),
    CatalogEntry("timescaledb",    "database", "infra", (), "wikidata:Q56268420"),
    CatalogEntry("questdb",        "database", "infra", (), "github-topic:questdb"),
    CatalogEntry("cockroachdb",    "database", "infra", ("cockroach",), "wikidata:Q27966145"),
    CatalogEntry("yugabytedb",     "database", "infra", ("yugabyte",), "github-topic:yugabyte"),
    CatalogEntry("vitess",         "database", "infra", (), "wikidata:Q24938655"),
    CatalogEntry("rds",            "database", "infra", ("amazon rds", "aws rds"), "wikidata:Q1430347"),
    CatalogEntry("aurora",         "database", "infra", ("amazon aurora", "aws aurora"), "wikidata:Q50813948"),
    CatalogEntry("cosmos db",      "database", "infra", ("cosmosdb", "azure cosmos", "azure cosmos db"), "wikidata:Q28797178"),
    CatalogEntry("dynamodb",       "database", "infra", ("amazon dynamodb", "aws dynamodb"), "wikidata:Q1071728"),
    # Vector / search
    CatalogEntry("pinecone",       "database", "infra", (), "github-topic:pinecone"),
    CatalogEntry("qdrant",         "database", "infra", (), "github-topic:qdrant"),
    CatalogEntry("weaviate",       "database", "infra", (), "github-topic:weaviate"),
    CatalogEntry("chroma",         "database", "infra", ("chromadb",), "github-topic:chroma"),
    CatalogEntry("milvus",         "database", "infra", (), "github-topic:milvus"),
    # ELK stack components
    CatalogEntry("logstash",       "database", "infra", (), "wikidata:Q19892385"),
    CatalogEntry("kibana",         "database", "infra", (), "wikidata:Q17091601"),
    # Graph
    CatalogEntry("neo4j",          "database", "infra", (), "wikidata:Q1628290"),
    CatalogEntry("arangodb",       "database", "infra", (), "wikidata:Q17004014"),
    CatalogEntry("dgraph",         "database", "infra", (), "github-topic:dgraph"),
    # Emerging SQLite-in-cloud
    CatalogEntry("turso",          "database", "infra", (), "github-topic:turso"),
    CatalogEntry("libsql",         "database", "infra", (), "github-topic:libsql"),
)


# ── Platforms (50+) ─────────────────────────────────────────────────
# "runtime / orchestration environment where apps run"

_PLATFORMS: tuple[CatalogEntry, ...] = (
    # Containers / orchestration
    CatalogEntry("docker",         "platform", "infra", (), "wikidata:Q15206305"),
    CatalogEntry("docker swarm",   "platform", "infra", ("swarm",), "wikipedia:Docker_Swarm"),
    CatalogEntry("podman",         "platform", "infra", (), "wikidata:Q64086710"),
    CatalogEntry("containerd",     "platform", "infra", (), "github-topic:containerd"),
    CatalogEntry("cri-o",          "platform", "infra", ("crio",), "github-topic:cri-o"),
    CatalogEntry("kubernetes",     "platform", "infra", ("k8s",), "wikidata:Q22661306"),
    CatalogEntry("nomad",          "platform", "infra", ("hashicorp nomad",), "wikidata:Q29106998"),
    CatalogEntry("ecs",            "platform", "infra", ("aws ecs", "amazon ecs"), "wikidata:Q17044923"),
    CatalogEntry("openshift",      "platform", "infra", ("red hat openshift",), "wikidata:Q3046363"),
    CatalogEntry("rancher",        "platform", "infra", (), "wikidata:Q58708580"),
    CatalogEntry("mesos",          "platform", "infra", ("apache mesos",), "wikidata:Q14911018"),
    CatalogEntry("mesosphere dc/os", "platform", "infra", ("dc/os", "mesosphere",), "wikipedia:DC/OS"),

    # Cloud providers (IaaS + PaaS)
    CatalogEntry("aws",            "platform", "infra", ("amazon web services",), "wikidata:Q2283"),
    CatalogEntry("gcp",            "platform", "infra", ("google cloud platform", "google cloud"), "wikidata:Q19841877"),
    CatalogEntry("azure",          "platform", "infra", ("microsoft azure",), "wikidata:Q725967"),
    CatalogEntry("alibaba cloud",  "platform", "infra", ("aliyun",), "wikidata:Q18215835"),
    CatalogEntry("ibm cloud",      "platform", "infra", (), "wikidata:Q6885230"),
    CatalogEntry("oracle cloud",   "platform", "infra", ("oci",), "wikidata:Q20058117"),
    CatalogEntry("digitalocean",   "platform", "infra", ("digital ocean",), "wikidata:Q5273803"),
    CatalogEntry("linode",         "platform", "infra", (), "wikidata:Q1827817"),
    CatalogEntry("vultr",          "platform", "infra", (), "wikidata:Q56401713"),
    CatalogEntry("hetzner",        "platform", "infra", (), "wikidata:Q897222"),
    CatalogEntry("scaleway",       "platform", "infra", (), "wikidata:Q25113110"),

    # PaaS / app hosting
    CatalogEntry("heroku",         "platform", "infra", (), "wikidata:Q1393724"),
    CatalogEntry("vercel",         "platform", "infra", (), "wikidata:Q101475428"),
    CatalogEntry("netlify",        "platform", "infra", (), "wikidata:Q59480946"),
    CatalogEntry("fly.io",         "platform", "infra", ("flyio",), "github-topic:fly-io"),
    CatalogEntry("railway",        "platform", "infra", (), "github-topic:railwayapp"),
    CatalogEntry("render",         "platform", "infra", (), "github-topic:render"),
    CatalogEntry("deno deploy",    "platform", "infra", (), "github-topic:deno-deploy"),
    CatalogEntry("cloudflare pages","platform","infra", (), "github-topic:cloudflare-pages"),
    CatalogEntry("github pages",   "platform", "infra", (), "wikidata:Q29005170"),
    CatalogEntry("fastmail",       "platform", "infra", (), "github-topic:fastmail"),

    # Serverless / edge compute
    CatalogEntry("aws lambda",     "platform", "infra", ("lambda",), "wikidata:Q19841877"),
    CatalogEntry("cloud run",      "platform", "infra", ("google cloud run",), "wikidata:Q66075540"),
    CatalogEntry("azure functions","platform", "infra", (), "wikidata:Q29138330"),
    CatalogEntry("cloudflare workers","platform","infra", (), "github-topic:cloudflare-workers"),
    CatalogEntry("fastly compute", "platform", "infra", ("compute@edge",), "github-topic:fastly"),
    CatalogEntry("modal",          "platform", "infra", ("modal.com",), "github-topic:modal-labs"),
    CatalogEntry("replicate",      "platform", "infra", (), "github-topic:replicate"),

    # CDN / edge
    CatalogEntry("cloudflare",     "platform", "infra", (), "wikidata:Q5176426"),
    CatalogEntry("fastly",         "platform", "infra", (), "wikidata:Q4000559"),
    CatalogEntry("akamai",         "platform", "infra", ("akamai technologies",), "wikidata:Q374715"),

    # ── v9 coverage-audit backfill ──
    # Managed Kubernetes offerings
    CatalogEntry("eks",            "platform", "infra", ("amazon eks", "aws eks"), "wikipedia:Amazon_Elastic_Kubernetes_Service"),
    CatalogEntry("gke",            "platform", "infra", ("google kubernetes engine",), "wikipedia:Google_Kubernetes_Engine"),
    CatalogEntry("aks",            "platform", "infra", ("azure kubernetes service",), "wikipedia:Azure_Kubernetes_Service"),
    # Serverless compute (missing)
    CatalogEntry("fargate",        "platform", "infra", ("aws fargate",), "wikipedia:AWS_Fargate"),
    CatalogEntry("app engine",     "platform", "infra", ("google app engine", "gae"), "wikidata:Q1115120"),
    CatalogEntry("cloud functions","platform", "infra", ("google cloud functions",), "wikipedia:Google_Cloud_Functions"),
    CatalogEntry("beam",           "platform", "infra", ("google cloud beam", "apache beam"), "wikidata:Q27517264"),
    # App platforms
    CatalogEntry("app runner",     "platform", "infra", ("aws app runner",), "wikipedia:AWS_App_Runner"),
    CatalogEntry("elastic beanstalk","platform", "infra", ("aws elastic beanstalk", "beanstalk"), "wikidata:Q2496321"),
    # Supabase / Firebase etc (backend platforms)
    CatalogEntry("supabase",       "platform", "infra", (), "wikidata:Q110178064"),
    CatalogEntry("firebase",       "platform", "infra", (), "wikidata:Q17053695"),
    CatalogEntry("planetscale",    "platform", "infra", (), "github-topic:planetscale"),
)


# ── Tools (170+) ────────────────────────────────────────────────────
# "dev-time tooling that does NOT run in production"

_TOOLS: tuple[CatalogEntry, ...] = (
    # Linters / formatters / type checkers
    CatalogEntry("ruff",           "tool", "tooling", (), "github-topic:ruff"),
    CatalogEntry("black",          "tool", "tooling", (), "github-topic:black-formatter"),
    CatalogEntry("mypy",           "tool", "tooling", (), "wikidata:Q17098997"),
    CatalogEntry("pyright",        "tool", "tooling", (), "github-topic:pyright"),
    CatalogEntry("pyre",           "tool", "tooling", (), "github-topic:pyre-check"),
    CatalogEntry("pylint",         "tool", "tooling", (), "github-topic:pylint"),
    CatalogEntry("flake8",         "tool", "tooling", (), "github-topic:flake8"),
    CatalogEntry("eslint",         "tool", "tooling", (), "wikidata:Q22065712"),
    CatalogEntry("prettier",       "tool", "tooling", (), "github-topic:prettier"),
    CatalogEntry("biome",          "tool", "tooling", ("biomejs",), "github-topic:biome"),
    CatalogEntry("dprint",         "tool", "tooling", (), "github-topic:dprint"),
    CatalogEntry("rubocop",        "tool", "tooling", (), "wikidata:Q19842773"),
    CatalogEntry("golangci-lint",  "tool", "tooling", (), "github-topic:golangci-lint"),
    CatalogEntry("swiftlint",      "tool", "tooling", (), "github-topic:swiftlint"),
    CatalogEntry("clang-tidy",     "tool", "tooling", (), "wikipedia:clang-tidy"),
    CatalogEntry("shellcheck",     "tool", "tooling", (), "wikidata:Q24890830"),
    CatalogEntry("yamllint",       "tool", "tooling", (), "github-topic:yamllint"),
    CatalogEntry("hadolint",       "tool", "tooling", (), "github-topic:hadolint"),

    # Package / build tools
    CatalogEntry("poetry",         "tool", "tooling", ("python poetry",), "github-topic:python-poetry"),
    CatalogEntry("uv",             "tool", "tooling", ("astral uv",), "github-topic:uv"),
    CatalogEntry("pip",            "tool", "tooling", (), "wikidata:Q13507421"),
    CatalogEntry("pip-tools",      "tool", "tooling", (), "github-topic:pip-tools"),
    CatalogEntry("pdm",            "tool", "tooling", (), "github-topic:pdm"),
    CatalogEntry("conda",          "tool", "tooling", ("miniconda", "anaconda"), "wikidata:Q15270566"),
    CatalogEntry("pipenv",         "tool", "tooling", (), "github-topic:pipenv"),
    CatalogEntry("npm",            "tool", "tooling", (), "wikidata:Q7637948"),
    CatalogEntry("yarn",           "tool", "tooling", ("yarn pkg",), "wikidata:Q29910317"),
    CatalogEntry("pnpm",           "tool", "tooling", (), "github-topic:pnpm"),
    CatalogEntry("cargo",          "tool", "tooling", ("rust cargo",), "wikipedia:Cargo_(software)"),
    CatalogEntry("bundler",        "tool", "tooling", (), "wikidata:Q20027267"),
    CatalogEntry("go modules",     "tool", "tooling", ("gomod",), "github-topic:go-modules"),
    CatalogEntry("maven",          "tool", "tooling", ("apache maven",), "wikidata:Q47275"),
    CatalogEntry("gradle",         "tool", "tooling", (), "wikidata:Q914807"),
    CatalogEntry("sbt",            "tool", "tooling", ("scala sbt",), "github-topic:sbt"),
    CatalogEntry("mix",            "tool", "tooling", ("elixir mix",), "github-topic:elixir-mix"),
    CatalogEntry("hex",            "tool", "tooling", (), "github-topic:elixir-hex"),
    CatalogEntry("apt",            "tool", "tooling", ("apt-get",), "wikidata:Q2379284"),
    CatalogEntry("brew",           "tool", "tooling", ("homebrew",), "wikidata:Q12032788"),
    CatalogEntry("pacman",         "tool", "tooling", (), "wikidata:Q380338"),
    CatalogEntry("nix",            "tool", "tooling", ("nixpkgs",), "wikidata:Q6961957"),
    CatalogEntry("bazel",          "tool", "tooling", (), "github-topic:bazel"),
    CatalogEntry("buck2",          "tool", "tooling", ("buck",), "github-topic:buck2"),
    CatalogEntry("nx",             "tool", "tooling", ("nrwl/nx",), "github-topic:nx"),
    CatalogEntry("lerna",          "tool", "tooling", (), "github-topic:lerna"),
    CatalogEntry("rush",           "tool", "tooling", ("microsoft rush",), "github-topic:rushstack"),
    CatalogEntry("make",           "tool", "tooling", ("gnu make", "makefile"), "wikidata:Q206212"),
    CatalogEntry("cmake",          "tool", "tooling", (), "wikidata:Q205817"),
    CatalogEntry("ninja",          "tool", "tooling", ("ninja-build",), "github-topic:ninja-build"),

    # Editors / IDEs
    CatalogEntry("vs code",        "tool", "tooling", ("vscode", "visual studio code"), "wikidata:Q19841877"),
    CatalogEntry("cursor",         "tool", "tooling", ("cursor editor",), "github-topic:cursor"),
    CatalogEntry("zed",            "tool", "tooling", ("zed editor",), "github-topic:zed-editor"),
    CatalogEntry("helix",          "tool", "tooling", ("helix editor",), "github-topic:helix-editor"),
    CatalogEntry("neovim",         "tool", "tooling", ("nvim",), "wikidata:Q20129851"),
    CatalogEntry("vim",            "tool", "tooling", (), "wikidata:Q285116"),
    CatalogEntry("emacs",          "tool", "tooling", ("gnu emacs",), "wikidata:Q189722"),
    CatalogEntry("jetbrains ides", "tool", "tooling", ("jetbrains",), "wikidata:Q5143533"),
    CatalogEntry("intellij idea",  "tool", "tooling", ("intellij",), "wikidata:Q307774"),
    CatalogEntry("pycharm",        "tool", "tooling", (), "wikidata:Q3412961"),
    CatalogEntry("webstorm",       "tool", "tooling", (), "wikidata:Q7979826"),
    CatalogEntry("goland",         "tool", "tooling", (), "wikidata:Q33135464"),
    CatalogEntry("rubymine",       "tool", "tooling", (), "wikidata:Q7379380"),
    CatalogEntry("phpstorm",       "tool", "tooling", (), "wikidata:Q1362892"),
    CatalogEntry("clion",          "tool", "tooling", (), "wikidata:Q19611835"),
    CatalogEntry("android studio", "tool", "tooling", (), "wikidata:Q13402701"),
    CatalogEntry("xcode",          "tool", "tooling", (), "wikidata:Q206803"),
    CatalogEntry("sublime text",   "tool", "tooling", ("sublime",), "wikidata:Q1232886"),
    CatalogEntry("atom",           "tool", "tooling", ("atom editor",), "wikidata:Q15637192"),

    # Testing
    CatalogEntry("pytest",         "tool", "tooling", (), "wikidata:Q17089078"),
    CatalogEntry("unittest",       "tool", "tooling", ("python unittest",), "github-topic:unittest"),
    CatalogEntry("playwright",     "tool", "tooling", (), "github-topic:playwright"),
    CatalogEntry("jest",           "tool", "tooling", (), "wikidata:Q48755793"),
    CatalogEntry("vitest",         "tool", "tooling", (), "github-topic:vitest"),
    CatalogEntry("mocha",          "tool", "tooling", (), "github-topic:mocha"),
    CatalogEntry("jasmine",        "tool", "tooling", (), "wikidata:Q14923057"),
    CatalogEntry("karma",          "tool", "tooling", ("karma test runner",), "github-topic:karma-runner"),
    CatalogEntry("nightwatch",     "tool", "tooling", (), "github-topic:nightwatchjs"),
    CatalogEntry("testcafe",       "tool", "tooling", (), "github-topic:testcafe"),
    CatalogEntry("cypress",        "tool", "tooling", (), "github-topic:cypress"),
    CatalogEntry("selenium",       "tool", "tooling", ("selenium webdriver",), "wikidata:Q28128"),
    CatalogEntry("appium",         "tool", "tooling", (), "wikidata:Q27058780"),
    CatalogEntry("webdriverio",    "tool", "tooling", ("webdriver.io",), "github-topic:webdriverio"),
    CatalogEntry("puppeteer",      "tool", "tooling", (), "github-topic:puppeteer"),
    CatalogEntry("rspec",          "tool", "tooling", (), "wikidata:Q285988"),
    CatalogEntry("junit",          "tool", "tooling", (), "wikidata:Q767155"),
    CatalogEntry("testng",         "tool", "tooling", (), "wikidata:Q2994140"),
    CatalogEntry("k6",             "tool", "tooling", ("grafana k6",), "github-topic:k6"),
    CatalogEntry("locust",         "tool", "tooling", ("locust.io",), "github-topic:locustio"),
    CatalogEntry("jmeter",         "tool", "tooling", ("apache jmeter",), "wikidata:Q1399"),
    CatalogEntry("robot framework","tool", "tooling", ("robotframework",), "wikidata:Q12772517"),

    # Bundlers / transpilers
    CatalogEntry("webpack",        "tool", "tooling", (), "wikidata:Q25039180"),
    CatalogEntry("vite",           "tool", "tooling", (), "github-topic:vitejs"),
    CatalogEntry("esbuild",        "tool", "tooling", (), "github-topic:esbuild"),
    CatalogEntry("rollup",         "tool", "tooling", (), "github-topic:rollup"),
    CatalogEntry("parcel",         "tool", "tooling", (), "github-topic:parceljs"),
    CatalogEntry("swc",            "tool", "tooling", (), "github-topic:swc"),
    CatalogEntry("turbopack",      "tool", "tooling", (), "github-topic:turbopack"),
    CatalogEntry("babel",          "tool", "tooling", (), "wikidata:Q56060869"),
    CatalogEntry("tsc",            "tool", "tooling", ("typescript compiler",), "github-topic:typescript-compiler"),
    CatalogEntry("browserify",     "tool", "tooling", (), "github-topic:browserify"),

    # CI / CD
    CatalogEntry("github actions", "tool", "tooling", ("gha",), "wikidata:Q60909594"),
    CatalogEntry("jenkins",        "tool", "tooling", (), "wikidata:Q1320660"),
    CatalogEntry("circleci",       "tool", "tooling", (), "wikidata:Q21015043"),
    CatalogEntry("gitlab ci",      "tool", "tooling", ("gitlab-ci",), "wikidata:Q16653773"),
    CatalogEntry("buildkite",      "tool", "tooling", (), "github-topic:buildkite"),
    CatalogEntry("argocd",         "tool", "tooling", ("argo cd",), "github-topic:argo-cd"),
    CatalogEntry("drone",          "tool", "tooling", ("drone ci",), "github-topic:drone-ci"),
    CatalogEntry("travis",         "tool", "tooling", ("travis ci",), "wikidata:Q16654172"),
    CatalogEntry("bamboo",         "tool", "tooling", (), "wikidata:Q4853925"),
    CatalogEntry("teamcity",       "tool", "tooling", (), "wikidata:Q7698667"),
    CatalogEntry("spinnaker",      "tool", "tooling", (), "wikidata:Q27984049"),
    CatalogEntry("harness",        "tool", "tooling", (), "github-topic:harness"),
    CatalogEntry("codefresh",      "tool", "tooling", (), "github-topic:codefresh"),
    CatalogEntry("flux",           "tool", "tooling", ("fluxcd",), "github-topic:fluxcd"),

    # Observability
    CatalogEntry("grafana",        "tool", "tooling", (), "wikidata:Q21442905"),
    CatalogEntry("datadog",        "tool", "tooling", (), "wikidata:Q16930330"),
    CatalogEntry("sentry",         "tool", "tooling", ("sentry.io",), "wikidata:Q28121"),
    CatalogEntry("new relic",      "tool", "tooling", ("newrelic",), "wikidata:Q7013601"),
    CatalogEntry("opentelemetry",  "tool", "tooling", ("otel",), "github-topic:opentelemetry"),
    CatalogEntry("honeycomb",      "tool", "tooling", ("honeycomb.io",), "github-topic:honeycombio"),
    CatalogEntry("dynatrace",      "tool", "tooling", (), "wikidata:Q5317030"),
    CatalogEntry("appdynamics",    "tool", "tooling", (), "wikidata:Q14923046"),
    CatalogEntry("splunk",         "tool", "tooling", (), "wikidata:Q737761"),
    CatalogEntry("loki",           "tool", "tooling", ("grafana loki",), "github-topic:grafana-loki"),
    CatalogEntry("tempo",          "tool", "tooling", ("grafana tempo",), "github-topic:grafana-tempo"),
    CatalogEntry("jaeger",         "tool", "tooling", (), "github-topic:jaegertracing"),
    CatalogEntry("zipkin",         "tool", "tooling", (), "wikidata:Q30891090"),

    # Version control (client tools)
    CatalogEntry("git",            "tool", "tooling", (), "wikidata:Q186055"),
    CatalogEntry("mercurial",      "tool", "tooling", ("hg",), "wikidata:Q76059"),
    CatalogEntry("subversion",     "tool", "tooling", ("svn",), "wikidata:Q46794"),
    CatalogEntry("perforce",       "tool", "tooling", ("p4",), "wikidata:Q2003196"),
    CatalogEntry("lazygit",        "tool", "tooling", (), "github-topic:lazygit"),
    CatalogEntry("gh",             "tool", "tooling", ("github cli",), "github-topic:github-cli"),

    # Security / dep scanners
    CatalogEntry("snyk",           "tool", "tooling", (), "wikidata:Q29017038"),
    CatalogEntry("dependabot",     "tool", "tooling", (), "wikidata:Q50701945"),
    CatalogEntry("trivy",          "tool", "tooling", (), "github-topic:trivy"),
    CatalogEntry("checkov",        "tool", "tooling", (), "github-topic:checkov"),
    CatalogEntry("semgrep",        "tool", "tooling", (), "github-topic:semgrep"),

    # Developer experience
    CatalogEntry("docker compose", "tool", "tooling", ("compose",), "wikidata:Q47397562"),
    CatalogEntry("bitwarden",      "tool", "tooling", (), "wikidata:Q50954912"),
    CatalogEntry("1password",      "tool", "tooling", (), "wikidata:Q1751954"),
    CatalogEntry("fzf",            "tool", "tooling", (), "github-topic:fzf"),
    CatalogEntry("ripgrep",        "tool", "tooling", ("rg",), "github-topic:ripgrep"),
    CatalogEntry("bat",            "tool", "tooling", (), "github-topic:bat"),
    CatalogEntry("delta",          "tool", "tooling", ("git-delta",), "github-topic:git-delta"),
    CatalogEntry("htop",           "tool", "tooling", (), "wikidata:Q2032758"),
    CatalogEntry("direnv",         "tool", "tooling", (), "github-topic:direnv"),
    CatalogEntry("pre-commit",     "tool", "tooling", (), "github-topic:pre-commit"),
    CatalogEntry("terraform",      "tool", "tooling", (), "wikidata:Q28030856"),
    CatalogEntry("ansible",        "tool", "tooling", (), "wikidata:Q2852503"),
    CatalogEntry("puppet",         "tool", "tooling", (), "wikidata:Q1570627"),
    CatalogEntry("chef",           "tool", "tooling", (), "wikidata:Q2972087"),
    CatalogEntry("pulumi",         "tool", "tooling", (), "wikidata:Q58753651"),
    CatalogEntry("packer",         "tool", "tooling", (), "github-topic:packer"),
    CatalogEntry("vault",          "tool", "tooling", ("hashicorp vault",), "wikidata:Q56406408"),

    # ── v9 coverage-audit backfill (MSEB + modern additions) ──
    # Monitoring / observability (heavily mentioned in ADR corpora)
    CatalogEntry("prometheus",     "tool", "tooling", (), "wikidata:Q20620649"),
    CatalogEntry("alertmanager",   "tool", "tooling", (), "github-topic:alertmanager"),
    CatalogEntry("pagerduty",      "tool", "tooling", (), "wikidata:Q16927655"),
    CatalogEntry("opsgenie",       "tool", "tooling", (), "wikidata:Q24916441"),
    CatalogEntry("datadog",        "tool", "tooling", (), "wikidata:Q17078765"),
    CatalogEntry("new relic",      "tool", "tooling", (), "wikidata:Q7015562"),
    CatalogEntry("splunk",         "tool", "tooling", (), "wikidata:Q2307714"),
    CatalogEntry("sentry",         "tool", "tooling", (), "wikidata:Q56062459"),
    CatalogEntry("honeycomb",      "tool", "tooling", ("honeycomb.io",), "github-topic:honeycomb"),
    CatalogEntry("grafana loki",   "tool", "tooling", ("loki",), "github-topic:grafana-loki"),
    CatalogEntry("jaeger",         "tool", "tooling", (), "wikidata:Q62005184"),
    CatalogEntry("zipkin",         "tool", "tooling", (), "wikidata:Q24875049"),
    CatalogEntry("opentelemetry",  "tool", "tooling", ("otel",), "github-topic:opentelemetry"),
    # Communication / collab (used in dev workflows)
    CatalogEntry("slack",          "tool", "tooling", (), "wikidata:Q16996553"),
    CatalogEntry("microsoft teams","tool", "tooling", ("ms teams", "teams"), "wikidata:Q28406404"),
    CatalogEntry("discord",        "tool", "tooling", (), "wikidata:Q28490564"),
    CatalogEntry("zoom",           "tool", "tooling", ("zoom meetings",), "wikidata:Q39095085"),
    # CI/CD + build
    CatalogEntry("buildkite",      "tool", "tooling", (), "wikidata:Q107537540"),
    CatalogEntry("turborepo",      "tool", "tooling", ("turbo",), "github-topic:turborepo"),
    CatalogEntry("nx",             "tool", "tooling", ("nx monorepo",), "github-topic:nx"),
    CatalogEntry("rush",           "tool", "tooling", (), "github-topic:rushjs"),
    CatalogEntry("lerna",          "tool", "tooling", (), "wikidata:Q104772275"),
    CatalogEntry("bazel",          "tool", "tooling", (), "wikidata:Q26938940"),
    CatalogEntry("dagger",         "tool", "tooling", (), "github-topic:dagger"),
    CatalogEntry("earthly",        "tool", "tooling", (), "github-topic:earthly"),
    # Python toolchain gaps
    CatalogEntry("hatch",          "tool", "tooling", (), "github-topic:hatch"),
    CatalogEntry("pdm",            "tool", "tooling", (), "github-topic:pdm"),
    CatalogEntry("rye",            "tool", "tooling", (), "github-topic:rye"),
    # JS / TS toolchain gaps
    CatalogEntry("biome",          "tool", "tooling", (), "github-topic:biome"),
    CatalogEntry("rome",           "tool", "tooling", (), "github-topic:rome"),
    CatalogEntry("swc",            "tool", "tooling", (), "github-topic:swc"),
    CatalogEntry("esbuild",        "tool", "tooling", (), "github-topic:esbuild"),
    CatalogEntry("bun",            "tool", "tooling", ("bun.sh",), "github-topic:bun"),
    CatalogEntry("pnpm",           "tool", "tooling", (), "wikidata:Q89920306"),
    # Secrets / security
    CatalogEntry("confidant",      "tool", "tooling", (), "github-topic:confidant"),
    CatalogEntry("1password",      "tool", "tooling", ("1password cli",), "wikidata:Q2636858"),
    CatalogEntry("sops",           "tool", "tooling", (), "github-topic:sops"),
    # AI coding assistants (now a significant part of dev workflow)
    CatalogEntry("copilot",        "tool", "tooling", ("github copilot",), "wikidata:Q108139345"),
    CatalogEntry("chatgpt",        "tool", "tooling", (), "wikidata:Q115564437"),
    CatalogEntry("claude",         "tool", "tooling", ("claude code",), "wikidata:Q118093204"),
    CatalogEntry("cursor",         "tool", "tooling", ("cursor editor",), "github-topic:cursor"),
    CatalogEntry("codeium",        "tool", "tooling", (), "github-topic:codeium"),
    CatalogEntry("tabnine",        "tool", "tooling", (), "wikidata:Q85784555"),
    CatalogEntry("windsurf",       "tool", "tooling", (), "github-topic:windsurf"),
)


# ── Patterns (45+) ──────────────────────────────────────────────────
# "architectural or coding pattern AS A NAMED PATTERN"

_PATTERNS: tuple[CatalogEntry, ...] = (
    # Concurrency / async
    CatalogEntry("async/await",    "pattern", "language_runtime", ("async-await",), "wikipedia:Async/await"),
    CatalogEntry("event-loop concurrency", "pattern", "language_runtime", ("event-loop",), "wikipedia:Event_loop"),
    CatalogEntry("threads",        "pattern", "language_runtime", ("multithreading",), "wikipedia:Thread_(computing)"),
    CatalogEntry("fibers",         "pattern", "language_runtime", (), "wikipedia:Fiber_(computer_science)"),
    CatalogEntry("callback-style code", "pattern", "language_runtime", ("callbacks",), "wikipedia:Callback_(computer_programming)"),
    CatalogEntry("promise-based flow", "pattern", "language_runtime", ("promises",), "wikipedia:Futures_and_promises"),
    CatalogEntry("reactive streams","pattern", "language_runtime", ("reactive-streams",), "wikipedia:Reactive_programming"),
    CatalogEntry("actor model",    "pattern", "language_runtime", (), "wikipedia:Actor_model"),
    CatalogEntry("csp",            "pattern", "language_runtime", ("communicating sequential processes",), "wikipedia:Communicating_sequential_processes"),

    # Architectural
    CatalogEntry("microservices",  "pattern", "language_runtime", ("microservice architecture",), "wikipedia:Microservices"),
    CatalogEntry("monolith-first", "pattern", "language_runtime", ("monolithic architecture", "monolith"), "wikipedia:Monolithic_architecture"),
    CatalogEntry("hexagonal architecture", "pattern", "language_runtime", ("ports and adapters",), "wikipedia:Hexagonal_architecture_(software)"),
    CatalogEntry("clean architecture", "pattern", "language_runtime", (), "wikipedia:Clean_architecture"),
    CatalogEntry("onion architecture", "pattern", "language_runtime", (), "wikipedia:Onion_architecture"),
    CatalogEntry("service mesh",   "pattern", "language_runtime", (), "wikipedia:Service_mesh"),
    CatalogEntry("event-driven architecture","pattern","language_runtime", ("eda",), "wikipedia:Event-driven_architecture"),
    CatalogEntry("modular monolith","pattern","language_runtime", (), "github-topic:modular-monolith"),

    # Messaging / data
    CatalogEntry("pub/sub",        "pattern", "language_runtime", ("pubsub", "publish-subscribe"), "wikipedia:Publish–subscribe_pattern"),
    CatalogEntry("cqrs",           "pattern", "language_runtime", ("command query responsibility segregation",), "wikipedia:Command_Query_Responsibility_Segregation"),
    CatalogEntry("event sourcing", "pattern", "language_runtime", (), "wikipedia:Event_sourcing"),
    CatalogEntry("saga pattern",   "pattern", "language_runtime", ("saga",), "github-topic:saga-pattern"),

    # Design patterns (GoF)
    CatalogEntry("singleton",      "pattern", "language_runtime", ("singleton pattern",), "wikipedia:Singleton_pattern"),
    CatalogEntry("factory",        "pattern", "language_runtime", ("factory pattern", "factory method"), "wikipedia:Factory_method_pattern"),
    CatalogEntry("observer pattern","pattern","language_runtime", ("observer",), "wikipedia:Observer_pattern"),
    CatalogEntry("strategy pattern","pattern","language_runtime", ("strategy",), "wikipedia:Strategy_pattern"),
    CatalogEntry("decorator",      "pattern", "language_runtime", ("decorator pattern",), "wikipedia:Decorator_pattern"),
    CatalogEntry("adapter pattern","pattern", "language_runtime", ("adapter",), "wikipedia:Adapter_pattern"),
    CatalogEntry("facade pattern", "pattern", "language_runtime", ("facade",), "wikipedia:Facade_pattern"),
    CatalogEntry("proxy pattern",  "pattern", "language_runtime", ("proxy",), "wikipedia:Proxy_pattern"),
    CatalogEntry("command pattern","pattern", "language_runtime", ("command",), "wikipedia:Command_pattern"),
    CatalogEntry("iterator pattern","pattern", "language_runtime", ("iterator",), "wikipedia:Iterator_pattern"),
    CatalogEntry("state pattern",  "pattern", "language_runtime", ("state",), "wikipedia:State_pattern"),
    CatalogEntry("template method","pattern", "language_runtime", (), "wikipedia:Template_method_pattern"),
    CatalogEntry("visitor pattern","pattern", "language_runtime", ("visitor",), "wikipedia:Visitor_pattern"),
    CatalogEntry("composite pattern","pattern","language_runtime", ("composite",), "wikipedia:Composite_pattern"),
    CatalogEntry("chain of responsibility","pattern","language_runtime", (), "wikipedia:Chain-of-responsibility_pattern"),

    # DDD + adjacent
    CatalogEntry("dependency injection","pattern","language_runtime", ("di",), "wikipedia:Dependency_injection"),
    CatalogEntry("inversion of control","pattern","language_runtime", ("ioc",), "wikipedia:Inversion_of_control"),
    CatalogEntry("repository pattern","pattern","language_runtime", ("repository",), "wikipedia:Repository_pattern"),
    CatalogEntry("unit of work",   "pattern", "language_runtime", (), "wikipedia:Unit_of_Work"),
    CatalogEntry("domain-driven design","pattern","language_runtime", ("ddd",), "wikipedia:Domain-driven_design"),

    # Dev practices
    CatalogEntry("test-driven development","pattern","language_runtime", ("tdd",), "wikipedia:Test-driven_development"),
    CatalogEntry("behavior-driven development","pattern","language_runtime", ("bdd",), "wikipedia:Behavior-driven_development"),

    # Rendering / delivery
    CatalogEntry("server-side rendering","pattern","language_runtime", ("ssr",), "wikipedia:Server-side_scripting"),
    CatalogEntry("client-side rendering","pattern","language_runtime", ("csr",), "wikipedia:Single-page_application"),
    CatalogEntry("static site generation","pattern","language_runtime", ("ssg",), "wikipedia:Static_web_page"),
    CatalogEntry("isomorphic rendering","pattern","language_runtime", ("isomorphic",), "github-topic:isomorphic-javascript"),
    CatalogEntry("progressive enhancement","pattern","language_runtime", (), "wikipedia:Progressive_enhancement"),

    # MVC family
    CatalogEntry("model-view-controller","pattern","language_runtime", ("mvc",), "wikipedia:Model–view–controller"),
    CatalogEntry("model-view-viewmodel","pattern","language_runtime", ("mvvm",), "wikipedia:Model–view–viewmodel"),
    CatalogEntry("model-view-presenter","pattern","language_runtime", ("mvp",), "wikipedia:Model–view–presenter"),

    # ── Microservices / distributed systems
    #    (Chris Richardson's canonical microservices.io catalog
    #     + Sam Newman + Martin Fowler's bliki.  These are the
    #     "what pattern did you pick for your microservice boundary
    #     discussion?" entries that show up constantly in ADRs.)
    CatalogEntry("backends for frontends", "pattern", "language_runtime",
        ("bff", "backend for frontend"), "samnewman:bff"),
    CatalogEntry("api gateway", "pattern", "language_runtime",
        (), "microservices.io:api-gateway"),
    CatalogEntry("service discovery", "pattern", "language_runtime",
        ("client-side discovery", "server-side discovery"),
        "microservices.io:service-discovery"),
    CatalogEntry("database per service", "pattern", "language_runtime",
        (), "microservices.io:database-per-service"),
    CatalogEntry("shared database", "pattern", "language_runtime",
        (), "microservices.io:shared-database"),
    CatalogEntry("api composition", "pattern", "language_runtime",
        (), "microservices.io:api-composition"),
    CatalogEntry("strangler fig", "pattern", "language_runtime",
        ("strangler pattern", "strangler application"),
        "fowler:StranglerFigApplication"),
    CatalogEntry("anti-corruption layer", "pattern", "language_runtime",
        ("acl",), "azure:anti-corruption-layer"),
    CatalogEntry("choreography", "pattern", "language_runtime",
        ("event choreography",), "microservices.io:sagas"),
    CatalogEntry("orchestration", "pattern", "language_runtime",
        ("saga orchestrator",), "microservices.io:sagas"),
    CatalogEntry("two-phase commit", "pattern", "language_runtime",
        ("2pc",), "wikipedia:Two-phase_commit_protocol"),
    CatalogEntry("eventual consistency", "pattern", "language_runtime",
        (), "wikipedia:Eventual_consistency"),
    CatalogEntry("circuit breaker", "pattern", "language_runtime",
        (), "fowler:CircuitBreaker"),
    CatalogEntry("retry with backoff", "pattern", "language_runtime",
        ("exponential backoff", "retry pattern", "retry-with-backoff"),
        "azure:retry"),
    CatalogEntry("bulkhead", "pattern", "language_runtime",
        ("bulkhead isolation",), "azure:bulkhead"),

    # ── Cloud operational (Azure Cloud Design Patterns catalog)
    CatalogEntry("sidecar", "pattern", "language_runtime",
        ("sidecar pattern",), "azure:sidecar"),
    CatalogEntry("ambassador", "pattern", "language_runtime",
        (), "azure:ambassador"),
    CatalogEntry("cache-aside", "pattern", "language_runtime",
        (), "azure:cache-aside"),
    CatalogEntry("materialized view", "pattern", "language_runtime",
        (), "azure:materialized-view"),
    CatalogEntry("rate limiting", "pattern", "language_runtime",
        ("throttling",), "azure:throttling"),
    CatalogEntry("leader election", "pattern", "language_runtime",
        (), "azure:leader-election"),
    CatalogEntry("health check", "pattern", "language_runtime",
        ("health endpoint monitoring",), "azure:health-endpoint-monitoring"),
    CatalogEntry("gateway aggregation", "pattern", "language_runtime",
        (), "azure:gateway-aggregation"),
    CatalogEntry("gateway offloading", "pattern", "language_runtime",
        (), "azure:gateway-offloading"),
    CatalogEntry("gateway routing", "pattern", "language_runtime",
        (), "azure:gateway-routing"),
    CatalogEntry("valet key", "pattern", "language_runtime",
        (), "azure:valet-key"),
    CatalogEntry("sharding", "pattern", "language_runtime",
        ("horizontal partitioning",), "azure:sharding"),
    CatalogEntry("pipes and filters", "pattern", "language_runtime",
        (), "azure:pipes-and-filters"),
    CatalogEntry("priority queue", "pattern", "language_runtime",
        (), "azure:priority-queue"),
    CatalogEntry("competing consumers", "pattern", "language_runtime",
        (), "azure:competing-consumers"),
    CatalogEntry("claim check", "pattern", "language_runtime",
        (), "azure:claim-check"),
    CatalogEntry("compensating transaction", "pattern", "language_runtime",
        (), "azure:compensating-transaction"),

    # ── Frontend architecture
    CatalogEntry("micro-frontends", "pattern", "language_runtime",
        ("micro frontend", "microfrontend"), "fowler:micro-frontends"),
    CatalogEntry("island architecture", "pattern", "language_runtime",
        ("islands architecture",), "patterns.dev:islands"),
    CatalogEntry("jamstack", "pattern", "language_runtime",
        (), "jamstack.org:about"),

    # ── Data / messaging
    CatalogEntry("transactional outbox", "pattern", "language_runtime",
        ("outbox pattern", "outbox"), "microservices.io:transactional-outbox"),
    CatalogEntry("change data capture", "pattern", "language_runtime",
        ("cdc",), "wikipedia:Change_data_capture"),
    CatalogEntry("dead letter queue", "pattern", "language_runtime",
        ("dlq",), "aws:sqs-dead-letter-queues"),
    CatalogEntry("idempotency key", "pattern", "language_runtime",
        ("idempotent consumer",), "microservices.io:idempotent-consumer"),

    # ── Security architecture
    CatalogEntry("zero trust", "pattern", "language_runtime",
        ("zero trust architecture", "zta"), "nist:sp-800-207"),
    CatalogEntry("defense in depth", "pattern", "language_runtime",
        (), "nist:csrc:defense-in-depth"),

    # ── v9 coverage-audit backfill (MSEB + process patterns) ──
    # Methodology
    CatalogEntry("agile", "pattern", "language_runtime",
        ("agile methodology", "agile software development"), "wikidata:Q471061"),
    CatalogEntry("scrum", "pattern", "language_runtime",
        (), "wikidata:Q863514"),
    CatalogEntry("kanban", "pattern", "language_runtime",
        (), "wikidata:Q1053531"),
    CatalogEntry("waterfall", "pattern", "language_runtime",
        ("waterfall model",), "wikidata:Q1064411"),
    CatalogEntry("xp", "pattern", "language_runtime",
        ("extreme programming",), "wikidata:Q184251"),
    CatalogEntry("tdd", "pattern", "language_runtime",
        ("test-driven development", "test driven development"), "wikidata:Q830132"),
    CatalogEntry("bdd", "pattern", "language_runtime",
        ("behavior-driven development", "behavior driven development"), "wikidata:Q828581"),
    CatalogEntry("ddd", "pattern", "language_runtime",
        ("domain-driven design", "domain driven design"), "wikidata:Q271218"),
    # CI / CD / DevOps
    CatalogEntry("devops", "pattern", "language_runtime",
        (), "wikidata:Q1224892"),
    CatalogEntry("ci/cd", "pattern", "language_runtime",
        ("ci", "cd", "continuous integration", "continuous deployment",
         "continuous delivery", "ci-cd", "ci cd"),
        "wikipedia:CI/CD"),
    CatalogEntry("gitops", "pattern", "language_runtime",
        (), "github-topic:gitops"),
    CatalogEntry("mlops", "pattern", "language_runtime",
        (), "wikipedia:MLOps"),
    CatalogEntry("sre", "pattern", "language_runtime",
        ("site reliability engineering",), "wikipedia:Site_reliability_engineering"),
    CatalogEntry("platform engineering", "pattern", "language_runtime",
        (), "github-topic:platform-engineering"),
    CatalogEntry("trunk-based development", "pattern", "language_runtime",
        ("trunk based development",), "wikipedia:Trunk-based_development"),
    CatalogEntry("gitflow", "pattern", "language_runtime",
        ("git flow",), "wikipedia:Git#Gitflow_workflow"),
    # Auth patterns (observed in MSEB)
    CatalogEntry("oauth", "pattern", "language_runtime",
        ("oauth2", "oauth 2.0", "oauth 2"), "wikidata:Q475294"),
    CatalogEntry("openid connect", "pattern", "language_runtime",
        ("oidc", "openid"), "wikidata:Q1139141"),
    CatalogEntry("saml", "pattern", "language_runtime",
        (), "wikidata:Q185353"),
    CatalogEntry("jwt", "pattern", "language_runtime",
        ("json web token",), "wikidata:Q18030489"),
    CatalogEntry("mtls", "pattern", "language_runtime",
        ("mutual tls",), "wikipedia:Mutual_authentication"),
    # API patterns
    CatalogEntry("graphql", "pattern", "language_runtime",
        (), "wikidata:Q25104379"),
    CatalogEntry("rest", "pattern", "language_runtime",
        ("restful", "rest api", "restful api"), "wikidata:Q652751"),
    CatalogEntry("grpc", "pattern", "language_runtime",
        (), "wikidata:Q27134141"),
    CatalogEntry("websocket", "pattern", "language_runtime",
        ("websockets",), "wikidata:Q379217"),
    CatalogEntry("server-sent events", "pattern", "language_runtime",
        ("sse",), "wikidata:Q6131954"),
    CatalogEntry("webhooks", "pattern", "language_runtime",
        ("webhook",), "wikidata:Q2101686"),
    # Data / content patterns
    CatalogEntry("elk stack", "pattern", "language_runtime",
        ("elk", "elastic stack"), "wikipedia:Elastic_Stack"),
    CatalogEntry("ai/ml", "pattern", "language_runtime",
        ("ai", "ml", "machine learning", "artificial intelligence"),
        "wikipedia:Artificial_intelligence"),
    CatalogEntry("rag", "pattern", "language_runtime",
        ("retrieval augmented generation", "retrieval-augmented generation"),
        "wikipedia:Retrieval-augmented_generation"),
    # Serialization / data formats
    CatalogEntry("json", "pattern", "language_runtime",
        (), "wikidata:Q2063"),
    CatalogEntry("yaml", "pattern", "language_runtime",
        (), "wikidata:Q281876"),
    CatalogEntry("xml", "pattern", "language_runtime",
        (), "wikidata:Q2115"),
    CatalogEntry("toml", "pattern", "language_runtime",
        (), "wikidata:Q28449455"),
    CatalogEntry("csv", "pattern", "language_runtime",
        (), "wikidata:Q935809"),
    # Frontend patterns
    CatalogEntry("spa", "pattern", "language_runtime",
        ("single page application", "single-page application"), "wikidata:Q1571410"),
    CatalogEntry("ssr", "pattern", "language_runtime",
        ("server-side rendering", "server side rendering"), "wikipedia:Server-side_scripting"),
    CatalogEntry("jamstack", "pattern", "language_runtime",
        (), "wikipedia:Jamstack"),
    CatalogEntry("pwa", "pattern", "language_runtime",
        ("progressive web app",), "wikidata:Q22965425"),
    # Role / architectural layers (common ADR terminology)
    CatalogEntry("frontend", "pattern", "language_runtime",
        ("front-end", "front end"), "wikipedia:Frontend_and_backend"),
    CatalogEntry("backend", "pattern", "language_runtime",
        ("back-end", "back end"), "wikipedia:Frontend_and_backend"),
    CatalogEntry("fullstack", "pattern", "language_runtime",
        ("full-stack", "full stack"), "wikipedia:Full-stack_development"),
)


# ── Frequency (shared, time-interval only) ──────────────────────────

_FREQUENCIES: tuple[CatalogEntry, ...] = (
    CatalogEntry("before every commit","frequency","tooling", (), "rule:dev-workflow-cadence"),
    CatalogEntry("on save",            "frequency","tooling", (), "rule:dev-workflow-cadence"),
    CatalogEntry("every morning",      "frequency","tooling", (), "rule:daily-cadence"),
    CatalogEntry("every sprint",       "frequency","tooling", (), "rule:sprint-cadence"),
    CatalogEntry("in every pr",        "frequency","tooling", (), "rule:ci-cadence"),
    CatalogEntry("during stand-up",    "frequency","tooling", (), "rule:daily-cadence"),
    CatalogEntry("every release",      "frequency","tooling", (), "rule:release-cadence"),
    CatalogEntry("nightly",            "frequency","tooling", ("nightly in ci",), "rule:nightly-cadence"),
    CatalogEntry("on each push",       "frequency","tooling", (), "rule:ci-cadence"),
    CatalogEntry("after every migration","frequency","tooling", (), "rule:migration-cadence"),
    CatalogEntry("at feature-flag rollout","frequency","tooling", (), "rule:release-cadence"),
    CatalogEntry("at deploy time",     "frequency","tooling", (), "rule:deploy-cadence"),
    CatalogEntry("weekly",             "frequency","tooling", (), "rule:weekly-cadence"),
    CatalogEntry("hourly",             "frequency","tooling", (), "rule:hourly-cadence"),
    CatalogEntry("twice daily",        "frequency","tooling", (), "rule:twice-daily"),
    CatalogEntry("once a week",        "frequency","tooling", (), "rule:weekly-cadence"),
    CatalogEntry("daily",              "frequency","tooling", (), "rule:daily-cadence"),
    CatalogEntry("monthly",            "frequency","tooling", (), "rule:monthly-cadence"),
)


# ── Assemble the catalog ────────────────────────────────────────────

def _build_catalog(
    *groups: tuple[CatalogEntry, ...],
) -> dict[str, CatalogEntry]:
    """Index every canonical + alias to its entry; last-write-wins on conflict.

    We lowercase every key.  Duplicate canonicals or alias collisions
    are logged at import time by the caller (see
    :mod:`normalize.build_catalog_with_audit`).
    """
    out: dict[str, CatalogEntry] = {}
    for group in groups:
        for entry in group:
            out[entry.canonical.lower()] = entry
            for alias in entry.aliases:
                out[alias.lower()] = entry
    return out


CATALOG: dict[str, CatalogEntry] = _build_catalog(
    _LANGUAGES,
    _FRAMEWORKS,
    _LIBRARIES,
    _DATABASES,
    _PLATFORMS,
    _TOOLS,
    _PATTERNS,
    _FREQUENCIES,
)


# Keep the raw groups accessible for downstream consumers that need
# to filter by slot (e.g. building SDG pools).
ENTRIES_BY_SLOT: dict[str, tuple[CatalogEntry, ...]] = {
    "language":    _LANGUAGES,
    "framework":   _FRAMEWORKS,
    "library":     _LIBRARIES,
    "database":    _DATABASES,
    "platform":    _PLATFORMS,
    "tool":        _TOOLS,
    "pattern":     _PATTERNS,
    "frequency":   _FREQUENCIES,
}


__all__ = ["CATALOG", "ENTRIES_BY_SLOT"]
