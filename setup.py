"""
إعدادات تثبيت مشروع copytrade-aggregator — نسخ الصفقات المجمّع.

للتثبيت في وضع التطوير:
    pip install -e .

للتثبيت العادي:
    pip install .
"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

with open("requirements.txt", "r", encoding="utf-8") as fh:
    requirements = [line.strip() for line in fh if line.strip() and not line.startswith("#")]

setup(
    name="copytrade-aggregator",
    version="1.0.0",
    author="Trading Bot Team",
    description="نظام تجميع إشارات نسخ التداول من متداولين محترفين عبر منصات متعددة",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/jfjdjdieifne/copytrade-aggregator",
    license="MIT",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Financial and Insurance Industry",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Office/Business :: Financial :: Investment",
    ],
    python_requires=">=3.8",
    install_requires=requirements,
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-asyncio>=0.21.0",
            "black>=23.0.0",
            "flake8>=6.0.0",
            "mypy>=1.0.0",
        ],
    },
    project_urls={
        "Bug Reports": "https://github.com/jfjdjdieifne/copytrade-aggregator/issues",
        "Source": "https://github.com/jfjdjdieifne/copytrade-aggregator",
        "Documentation": "https://github.com/jfjdjdieifne/copytrade-aggregator#readme",
    },
)
