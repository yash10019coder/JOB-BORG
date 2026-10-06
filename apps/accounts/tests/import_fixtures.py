"""Synthetic resume text for the import tests.

Each fixture reproduces a *layout* seen in a corpus of real resumes (aggregate
measurements only; no real person or employer appears here): a pipe-separated
contact line with scheme-less links, an organization line followed by a
``Title - City, State  Mon YYYY - Mon YYYY`` line, date ranges inside Education,
skills written as glued ``Label: items`` lines, icon-font phone artifacts, and a
name followed by an ``Email :`` label. Everything is invented.
"""
from apps.accounts.importing.documents import normalize_text

# Layout A: bulleted organization line, then "Title - Location  dates"; "◦" detail bullets.
BULLET_ORG_RESUME = """\
Jane Doe
jane.doe@example.com | +91 9876543210 | linkedin.com/in/jane-doe-12345 | github.com/janedoe | janedoe.dev
Professional Summary
• Backend engineer with 8+ years building reliable payment and data services.
Professional Experience
• Acme Payments
Senior Backend Engineer - Pune, Maharashtra  Jan 2021 – Present
◦ Built payment APIs in Python and Django on AWS, serving millions of requests per day.
◦ Ran Kubernetes clusters and Terraform pipelines; added PostgreSQL read replicas.
• Globex Data
Software Engineer - Mumbai, Maharashtra  Jul 2017 – Dec 2020
◦ Wrote Go services and Kafka consumers; improved Redis caching.
Education
B.Tech in Computer Science, Example Institute of Technology  2013 – 2017
Technical Skills
Languages: Python, Go, SQL Cloud & Infra: AWS, Docker, Kubernetes, Terraform Databases: PostgreSQL, Redis
Tools: Git, Linux, Kafka
"""

# Layout: "Name Email : address", icon-font phone, MM/YYYY dates.
LABEL_NAME_RESUME = """\
John Smith Email : john.smith@example.com
♂phone+1 415 555 0134 | github.com/jsmith-dev
EXPERIENCE
Initech
Backend Developer
03/2019 - 11/2022
- Built internal tooling in Java and Spring Boot.
EDUCATION
BSc Computer Science 2014 - 2018
SKILLS
Java, Spring Boot, MySQL, Docker (Compose, Swarm), Git
"""

# ALL CAPS name, labelled links, "City, Country" header, "Skills:" inline.
CAPS_RESUME = """\
PRIYA RAMAN
Bengaluru, Karnataka, India | priya.raman@example.com | Tel: (+91) 98765 43210
Website: priyaraman.dev
LinkedIn: https://www.linkedin.com/in/priya-raman-77/
Technical Skills: Python, JavaScript, React.js, Node.js, k8s
Work Experience
Hooli
Staff Engineer
2020 - Present
"""

# No usable header at all: starts with a job title, then prose.
NO_HEADER_RESUME = """\
Software Engineer
Experienced engineer with a long history of shipping software for large teams and customers.
""" + "I enjoy solving hard problems across the stack and mentoring colleagues every week. " * 4 + "\n"

# A resume repeating a header on every page (noise) plus decoy digits.
REPEATED_HEADER_RESUME = """\
Alex Kim Resume
alex.kim@example.com | 020 7946 0958
Experience
Pied Piper
Engineer
Jan 2020 - Mar 2022
Alex Kim Resume
Raviga
Engineer
Apr 2022 - Present
Alex Kim Resume
"""


def normalized(text):
    return normalize_text(text)
