"""ModelGateway implementations."""

from super_agent.adapters.models.magpie import MagpieModel, MagpieModelGateway
from super_agent.adapters.models.openai_compatible import OpenAICompatibleGateway

__all__ = ["MagpieModel", "MagpieModelGateway", "OpenAICompatibleGateway"]

