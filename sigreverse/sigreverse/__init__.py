import logging

__all__ = [
    "io", "ldp3_client", "scoring", "robustness", "qc", "statistics",
    "cmap_algorithms", "drug_standardization", "dose_response", "fusion",
    "cache",
]
__version__ = "0.4.0"

# Configure package-level logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sigreverse")
