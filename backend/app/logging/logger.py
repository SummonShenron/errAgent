import os
import logging
import sys

NOISY_LOGGERS = (
    "uvicorn.access",
    "httpx",
    "httpcore",
    "h11",
    "anyio",
    "asyncio",
    "transformers",
    "huggingface_hub",
    "sentence_transformers",
    "chromadb",
)


def setup_logging():
    print("DEBUG: Logger setup is executing!")
    logger = logging.getLogger("errAgent Logger")
    
    if not logger.handlers:
        # Determine log level dynamically based on environment configuration
        env_log_level = os.getenv("LOG_LEVEL")
        
        if env_log_level:
            level = getattr(logging, env_log_level.upper(), logging.INFO)
        else:
            # Automatically set to DEBUG if local dev/mode is active, otherwise INFO
            is_local = (
                os.getenv("LOCAL_DEV", "false").lower() == "true" or 
                os.getenv("DEV_MODE", "false").lower() == "true"
            )
            level = logging.DEBUG if is_local else logging.INFO

        logger.setLevel(level)
        
        # Define format
        formatter = logging.Formatter(
            '%(levelname)s - %(message)s'
        )
        
        # Add a stream handler to ensure it outputs to terminal
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        
        logger.addHandler(handler)
        logger.propagate = False
        
        # Silence noisy third-party libraries
        for logger_name in NOISY_LOGGERS:
            logging.getLogger(logger_name).setLevel(logging.CRITICAL)     
            
        logging.getLogger("uvicorn.error").setLevel(logging.ERROR)
        logging.getLogger("uvicorn").setLevel(logging.ERROR)
        
    return logger