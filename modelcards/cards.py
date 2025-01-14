import re
import tempfile
from pathlib import Path
from typing import Optional, Union

import jinja2
import requests
import yaml
from huggingface_hub import hf_hub_download, upload_file
from huggingface_hub.utils.logging import get_logger

from .card_data import CardData, model_index_to_eval_results

TEMPLATE_MODELCARD_PATH = Path(__file__).parent / "modelcard_template.md"
REGEX_YAML_BLOCK = re.compile(
    r"---[\n\r]+([\S\s]*?)[\n\r]+---[\n\r]([\S\s].*)", re.DOTALL
)

logger = get_logger(__name__)


class RepoCard:
    def __init__(self, content: str):
        """Initialize a RepoCard from string content. The content should be a
        Markdown file with a YAML block at the beginning and a Markdown body.

        Args:
            content (str): The content of the Markdown file.

        Raises:
            ValueError: When the content of the repo card metadata is not found.
            ValueError: When the content of the repo card metadata is not a dictionary.
        """
        self.content = content
        match = REGEX_YAML_BLOCK.search(content)
        if match:
            # Metadata found in the YAML block
            yaml_block = match.group(1)
            self.text = match.group(2)
            data_dict = yaml.safe_load(yaml_block)

            # The YAML block's data should be a dictionary
            if not isinstance(data_dict, dict):
                raise ValueError("repo card metadata block should be a dict")
        else:
            # Model card without metadata... create empty metadata
            logger.warning(
                "Repo card metadata block was not found. Setting CardData to empty."
            )
            data_dict = {}
            self.text = content

        model_index = data_dict.pop("model-index", None)
        if model_index:
            try:
                model_name, eval_results = model_index_to_eval_results(model_index)
                data_dict["model_name"] = model_name
                data_dict["eval_results"] = eval_results
            except KeyError:
                logger.warning(
                    "Invalid model-index. Not loading eval results into CardData."
                )

        self.data = CardData(**data_dict)

    def __str__(self):
        return f"---\n{self.data.to_yaml()}\n---\n{self.text}"

    def save(self, filepath: Union[Path, str]):
        r"""Save a RepoCard to a file.

        Args:
            filepath (Union[Path, str]): Filepath to the markdown file to save.

        Example:
            >>> from modelcards import RepoCard
            >>> card = RepoCard("---\nlanguage: en\n---\n# This is a test repo card")
            >>> card.save("/tmp/test.md")
        """
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(str(self))

    @classmethod
    def load(cls, repo_id_or_path: Union[str, Path]):
        """Initialize a RepoCard from a Hugging Face Hub repo's README.md or a local filepath.

        Args:
            repo_id_or_path (Union[str, Path]):
                The repo ID associated with a Hugging Face Hub repo or a local filepath.

        Returns:
            modelcards.RepoCard: The RepoCard (or subclass) initialized from the repo's
                README.md file or filepath.

        Example:
            >>> from modelcards import RepoCard
            >>> card = RepoCard.load("nateraw/food")
            >>> assert card.data.tags == ["generated_from_trainer", "image-classification", "pytorch"]
        """
        if Path(repo_id_or_path).exists():
            card_path = Path(repo_id_or_path)
        else:
            card_path = hf_hub_download(repo_id_or_path, "README.md")

        return cls(Path(card_path).read_text())

    def validate(self, repo_type="model"):
        """Validates card against Hugging Face Hub's model card validation logic.
        Using this function requires access to the internet, so it is only called
        internally by `modelcards.ModelCard.push_to_hub`.

        Args:
            repo_type (str, *optional*):
                The type of Hugging Face repo to push to. Defaults to None, which will use
                use "model". Other options are "dataset" and "space".
        """
        if repo_type is None:
            repo_type = "model"

        # TODO - compare against repo types constant in huggingface_hub if we move this object there.
        if repo_type not in ["model", "space", "dataset"]:
            raise RuntimeError(
                "Provided repo_type '{repo_type}' should be one of ['model', 'space',"
                " 'dataset']."
            )

        body = {
            "repoType": repo_type,
            "content": str(self),
        }
        headers = {"Accept": "text/plain"}

        try:
            r = requests.post(
                "https://huggingface.co/validate-yaml", body, headers=headers
            )
            r.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            if r.status_code == 400:
                raise RuntimeError(r.text)
            else:
                raise exc

    def push_to_hub(
        self,
        repo_id,
        token=None,
        repo_type=None,
        commit_message=None,
        commit_description=None,
        create_pr=None,
    ):
        """Push a RepoCard to a Hugging Face Hub repo.

        Args:
            repo_id (str):
                The repo ID of the Hugging Face Hub repo to push to. Example: "nateraw/food".
            token (str, *optional*):
                Authentication token, obtained with `huggingface_hub.HfApi.login` method. Will default to
                the stored token.
            repo_type (str, *optional*):
                The type of Hugging Face repo to push to. Defaults to None, which will use
                use "model". Other options are "dataset" and "space".
            commit_message (str, *optional*):
                The summary / title / first line of the generated commit
            commit_description (str, *optional*)
                The description of the generated commit
            create_pr (bool, *optional*):
                Whether or not to create a Pull Request with this commit. Defaults to `False`.
        """
        repo_name = repo_id.split("/")[-1]

        if self.data.model_name and self.data.model_name != repo_name:
            logger.warning(
                f"Set model name {self.data.model_name} in CardData does not match "
                f"repo name {repo_name}. Updating model name to match repo name."
            )
            self.data.model_name = repo_name

        # Validate card before pushing to hub
        self.validate(repo_type=repo_type)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "README.md"
            tmp_path.write_text(str(self))
            upload_file(
                path_or_fileobj=str(tmp_path),
                path_in_repo="README.md",
                repo_id=repo_id,
                token=token,
                repo_type=repo_type,
                identical_ok=True,
                commit_message=commit_message,
                commit_description=commit_description,
                create_pr=create_pr,
            )


class ModelCard(RepoCard):
    @classmethod
    def from_template(
        cls,
        card_data: CardData,
        template_path: Optional[str] = TEMPLATE_MODELCARD_PATH,
        **template_kwargs,
    ):
        """Initialize a ModelCard from a template. By default, it uses the default template.

        Templates are Jinja2 templates that can be customized by passing keyword arguments.

        Args:
            card_data (modelcards.CardData, *required*):
                A modelcards.CardData instance containing the metadata you want to include in the YAML
                header of the model card on the Hugging Face Hub.
            template_path (str, *optional*):
                A path to a markdown file with optional Jinja template variables that can be filled
                in with `template_kwargs`. Defaults to the default template which can be found here:
                https://github.com/nateraw/modelcards/blob/main/modelcards/modelcard_template.md

        Returns:
            modelcards.ModelCard: A ModelCard instance with the specified card data and content from the
            template.

        Example:
            >>> from modelcards import ModelCard, CardData, EvalResult

            >>> # Using the Default Template
            >>> card_data = CardData(
            ...     language='en',
            ...     license='mit',
            ...     library_name='timm',
            ...     tags=['image-classification', 'resnet'],
            ...     datasets='beans',
            ...     metrics=['accuracy'],
            ... )
            >>> card = ModelCard.from_template(
            ...     card_data,
            ...     model_description='This model does x + y...'
            ... )

            >>> # Including Evaluation Results
            >>> card_data = CardData(
            ...     language='en',
            ...     tags=['image-classification', 'resnet'],
            ...     eval_results=[
            ...         EvalResult(
            ...             task_type='image-classification',
            ...             dataset_type='beans',
            ...             dataset_name='Beans',
            ...             metric_type='accuracy',
            ...             metric_value=0.9,
            ...         ),
            ...     ],
            ...     model_name='my-cool-model',
            ... )
            >>> card = ModelCard.from_template(card_data)

            >>> # Using a Custom Template
            >>> card_data = CardData(
            ...     language='en',
            ...     tags=['image-classification', 'resnet']
            ... )
            >>> card = ModelCard.from_template(
            ...     card_data=card_data,
            ...     template_path='./modelcards/modelcard_template.md',
            ...     custom_template_var='custom value',  # will be replaced in template if it exists
            ... )

        """
        content = jinja2.Template(Path(template_path).read_text()).render(
            card_data=card_data.to_yaml(), **template_kwargs
        )
        return cls(content)
