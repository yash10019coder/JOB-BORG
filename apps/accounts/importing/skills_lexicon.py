"""A small, versioned vocabulary of technologies (rules SK7, EA1-EA4, FK1-FK2, FU3).

The lexicon does three jobs: it gives skills a canonical spelling (``postgres`` ->
``PostgreSQL``), it lets an experience entry pick up skills that the resume never
listed in a Skills section, and it supplies the "dotted tech names" that must not
be mistaken for a portfolio domain (``Node.js`` is not a website).

It is **authored content, not derived data**: reviewed in the pull request that
adds or changes it, and ``SKILLS_LEXICON_VERSION`` is bumped on every change so
stored results can be told apart. A skill that is not here is still valid when
the resume declares it in its own Skills section; the lexicon only adds names.

``TAG_ALIASES`` maps skill spellings to *job-matching tags* (see
``apps.classification``); only tags that exist in the live ruleset and are not
role/level tags are ever imported into ``target_tags``.
"""
import re

SKILLS_LEXICON_VERSION = "2026-10-1"

# canonical name -> alternative spellings (all compared case-insensitively)
LEXICON = {
    # Languages
    "Python": ("py", "python3", "python 3", "cpython"),
    "JavaScript": ("js", "ecmascript", "es6", "es2015", "vanilla js"),
    "TypeScript": ("ts",),
    "Java": ("core java", "java 8", "java 11", "java 17"),
    "Kotlin": (),
    "Swift": (),
    "Objective-C": ("objc", "objective c"),
    "C": (),
    "C++": ("cpp", "c plus plus"),
    "C#": ("csharp", "c sharp"),
    "Go": ("golang", "go lang"),
    "Rust": (),
    "Ruby": (),
    "PHP": (),
    "Scala": (),
    "R": (),
    "Dart": (),
    "Julia": (),
    "Perl": (),
    "Lua": (),
    "Haskell": (),
    "Elixir": (),
    "Erlang": (),
    "Clojure": (),
    "Groovy": (),
    "MATLAB": (),
    "Bash": ("shell scripting", "shell script", "sh"),
    "PowerShell": (),
    "SQL": (),
    "PL/SQL": ("plsql",),
    "HTML": ("html5",),
    "CSS": ("css3",),
    "Sass": ("scss",),
    "Solidity": (),
    "Assembly": ("asm",),
    "COBOL": (),
    "Visual Basic": ("vb.net", "vba"),
    # Web and application frameworks
    "React": ("react.js", "reactjs"),
    "Redux": ("redux toolkit",),
    "Next.js": ("nextjs",),
    "Vue": ("vue.js", "vuejs"),
    "Nuxt.js": ("nuxtjs",),
    "Angular": ("angularjs", "angular.js"),
    "Svelte": ("sveltekit",),
    "jQuery": (),
    "Node.js": ("nodejs", "node js"),
    "Express": ("express.js", "expressjs"),
    "NestJS": ("nest.js",),
    "Django": ("django rest framework", "drf"),
    "Flask": (),
    "FastAPI": (),
    "Spring": ("spring boot", "springboot", "spring framework", "spring mvc"),
    "Hibernate": (),
    "Ruby on Rails": ("rails", "ror"),
    "Laravel": (),
    "Symfony": (),
    "ASP.NET": ("asp.net core", "aspnet"),
    ".NET": ("dotnet", ".net core", "dot net"),
    "Flutter": (),
    "React Native": ("reactnative",),
    "Android": ("android sdk", "android development"),
    "iOS": ("ios development",),
    "SwiftUI": (),
    "Jetpack Compose": (),
    "Tailwind CSS": ("tailwind", "tailwindcss"),
    "Bootstrap": (),
    "Material UI": ("mui", "material-ui"),
    "GraphQL": (),
    "REST": ("restful", "rest api", "restful api", "rest apis", "restful apis"),
    "gRPC": (),
    "WebSocket": ("websockets",),
    "Socket.io": ("socketio", "socket.io"),
    "Webpack": (),
    "Vite": (),
    "Storybook": (),
    # Data stores and messaging
    "PostgreSQL": ("postgres", "psql"),
    "MySQL": (),
    "SQLite": (),
    "MongoDB": ("mongo", "mongo db"),
    "Redis": (),
    "Elasticsearch": ("elastic search", "elk"),
    "Cassandra": ("apache cassandra",),
    "DynamoDB": ("dynamo db",),
    "Oracle": ("oracle db",),
    "SQL Server": ("mssql", "ms sql", "microsoft sql server"),
    "MariaDB": (),
    "Neo4j": (),
    "Firebase": ("firestore",),
    "Supabase": (),
    "Snowflake": (),
    "BigQuery": ("big query",),
    "Redshift": (),
    "ClickHouse": (),
    "Kafka": ("apache kafka",),
    "RabbitMQ": ("rabbit mq",),
    "Celery": (),
    "Airflow": ("apache airflow",),
    "Spark": ("apache spark", "pyspark"),
    "Hadoop": (),
    "Hive": (),
    "Flink": ("apache flink",),
    "dbt": (),
    # Data science and ML
    "Pandas": (),
    "NumPy": (),
    "SciPy": (),
    "scikit-learn": ("sklearn", "scikit learn"),
    "TensorFlow": ("tensorflow 2",),
    "PyTorch": ("torch",),
    "Keras": (),
    "XGBoost": (),
    "OpenCV": (),
    "NLTK": (),
    "spaCy": (),
    "LangChain": (),
    "Hugging Face": ("huggingface", "transformers"),
    "Machine Learning": ("ml",),
    "Deep Learning": (),
    "NLP": ("natural language processing",),
    "Computer Vision": (),
    "Tableau": (),
    "Power BI": ("powerbi",),
    "Excel": ("microsoft excel", "ms excel"),
    "Jupyter": ("jupyter notebook", "jupyter notebooks"),
    "Matplotlib": (),
    # Cloud, infrastructure and delivery
    "AWS": ("amazon web services",),
    "EC2": (),
    "S3": (),
    "Lambda": ("aws lambda",),
    "GCP": ("google cloud", "google cloud platform"),
    "Azure": ("microsoft azure",),
    "Docker": ("docker compose", "dockerfile"),
    "Kubernetes": ("k8s",),
    "Helm": (),
    "Terraform": (),
    "Ansible": (),
    "Chef": (),
    "Puppet": (),
    "Jenkins": (),
    "GitHub Actions": ("github action",),
    "GitLab CI": ("gitlab ci/cd", "gitlab-ci"),
    "CircleCI": (),
    "ArgoCD": ("argo cd",),
    "Prometheus": (),
    "Grafana": (),
    "Datadog": (),
    "Nginx": (),
    "Linux": (),
    "Unix": (),
    "Git": (),
    "GitHub": (),
    "GitLab": (),
    "Bitbucket": (),
    "Jira": (),
    "CI/CD": ("cicd", "ci cd", "ci-cd"),
    "Microservices": ("microservice", "micro services"),
    "Serverless": (),
    "Istio": (),
    "Vault": ("hashicorp vault",),
    "OpenShift": (),
    "Bazel": (),
    "Gradle": (),
    "Maven": (),
    # Testing
    "Jest": (),
    "pytest": ("py.test",),
    "JUnit": (),
    "Selenium": (),
    "Cypress": (),
    "Playwright": (),
    "Mocha": (),
    "Postman": (),
    "TDD": ("test driven development", "test-driven development"),
    # Other platforms and practices
    "OAuth": ("oauth2", "oauth 2.0"),
    "JWT": ("json web token", "json web tokens"),
    "Stripe": (),
    "Blockchain": (),
    "Ethereum": (),
    "Unity": (),
    "Unreal Engine": (),
    "Figma": (),
    "Agile": (),
    "Scrum": (),
    "Kanban": (),
    "System Design": (),
}

# Common English words (or very short names) that are also a technology. They
# attach to an entry only as the exact-case form AND when the resume declares
# them or lists them next to other technologies (rule EA3): "go to market" and
# "express interest" must never become skills.
AMBIGUOUS = frozenset(
    name.casefold()
    for name in (
        "Go", "R", "C", "Swift", "Rust", "Ruby", "Spark", "Scala", "Dart", "Julia",
        "Flask", "Express", "Spring", "Chef", "Puppet", "Rails", "Lambda", "Pandas",
        "Hive", "Vault", "Helm", "Cypress", "Mocha", "Jest", "Unity", "Oracle",
        "Kafka", "Agile", "Scrum", "Kanban", "Excel", "Android", "Git", "Bash", "Sh",
        "S3", "Transformers", "ML", "Torch", "Assembly", "Visual Basic", "Vite",
    )
)

# Names written with a dot that look like a web domain (rule FU3).
DOTTED_TECH = frozenset(
    {name.casefold() for name in LEXICON if "." in name}
    | {alias.casefold() for aliases in LEXICON.values() for alias in aliases if "." in alias}
    | {"vue.js", "react.js", "next.js", "express.js", "nest.js", "node.js", "d3.js",
       "three.js", "chart.js", "socket.io", "asp.net", "vb.net", ".net", "angular.js",
       "backbone.js", "ember.js", "alpine.js", "p5.js", "ml.net"}
)

# Skill spelling -> job-matching tag (rule FK1). Anything not listed has no tag.
TAG_ALIASES = {
    "python": "python", "py": "python", "python3": "python", "python 3": "python",
    "javascript": "javascript", "js": "javascript", "ecmascript": "javascript",
    "es6": "javascript",
    "go": "golang", "golang": "golang", "go lang": "golang",
    "kubernetes": "kubernetes", "k8s": "kubernetes",
}

_ALIAS_INDEX = {}
for _canonical, _aliases in LEXICON.items():
    _ALIAS_INDEX[_canonical.casefold()] = _canonical
    for _alias in _aliases:
        _ALIAS_INDEX.setdefault(_alias.casefold(), _canonical)

_VERSION_SUFFIX = re.compile(r"[\s-]*v?\d+(?:\.\d+)*$")


def canonical_skill(name):
    """The canonical spelling for a skill spelling, or ``None`` if unknown.

    Case-insensitive; a trailing version ("Python 3.11", "Java 17") is ignored.
    """
    key = re.sub(r"\s+", " ", (name or "").strip()).casefold()
    if not key:
        return None
    hit = _ALIAS_INDEX.get(key)
    if hit:
        return hit
    stripped = _VERSION_SUFFIX.sub("", key).strip()
    return _ALIAS_INDEX.get(stripped) if stripped and stripped != key else None


def is_ambiguous(name):
    return (name or "").casefold() in AMBIGUOUS


def all_spellings():
    """Every (spelling, canonical) pair, longest spelling first, so a matcher
    prefers ``Spring Boot`` over ``Spring`` and ``Node.js`` over ``Node``."""
    pairs = list(_ALIAS_INDEX.items())
    pairs.sort(key=lambda item: (-len(item[0]), item[0]))
    return pairs
