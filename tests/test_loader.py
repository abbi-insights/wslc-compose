import pytest

from wslc_compose.loader import ComposeError, load_project, parse_port

COMPOSE = """
name: demo
services:
  web:
    image: nginx:alpine
    ports:
      - "8088:80"
      - "127.0.0.1:9000:9000/udp"
      - target: 443
        published: 8443
    volumes:
      - ./html:/usr/share/nginx/html:ro
      - data:/data
    environment:
      - FOO=bar
      - NOVAL
    depends_on:
      - app
    networks:
      front:
        aliases: [www]
  app:
    image: alpine
    command: sh -c 'sleep 999'
    deploy:
      replicas: 2
      resources:
        limits:
          cpus: "0.5"
          memory: 256M
    networks: [front]
networks:
  front:
volumes:
  data:
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "compose.yaml").write_text(COMPOSE)
    (tmp_path / "html").mkdir()
    return load_project(str(tmp_path / "compose.yaml"))


def test_project_name_from_compose(project):
    assert project.name == "demo"


def test_networks_and_volumes_prefixed(project):
    assert project.networks["front"].name == "demo_front"
    assert project.volumes["data"].name == "demo_data"


def test_ports(project):
    web = project.services["web"]
    assert [p.to_flag() for p in web.ports] == ["8088:80", "127.0.0.1:9000:9000/udp", "8443:443"]


def test_volumes(project, tmp_path):
    web = project.services["web"]
    bind, named = web.volumes
    assert bind.type == "bind"
    assert bind.source == str(tmp_path / "html")
    assert bind.read_only
    assert named.type == "volume"
    assert named.source == "demo_data"  # resolved to project-scoped name


def test_environment_list_form(project):
    assert project.services["web"].environment == {"FOO": "bar", "NOVAL": None}


def test_command_string_is_split(project):
    assert project.services["app"].command == ["sh", "-c", "sleep 999"]


def test_deploy_limits(project):
    app = project.services["app"]
    assert app.replicas == 2
    assert app.cpus == "0.5"
    assert app.mem_limit == "256M"


def test_network_aliases(project):
    web = project.services["web"]
    assert web.networks == ["demo_front"]
    assert web.network_aliases["demo_front"] == ["www"]


def test_dependency_order(project):
    order = [s.name for s in project.sorted_services()]
    assert order.index("app") < order.index("web")


def test_container_names(project):
    web = project.services["web"]
    assert project.container_name(web, 1) == "demo-web-1"


def test_circular_dependency(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        """
services:
  a:
    image: x
    depends_on: [b]
  b:
    image: x
    depends_on: [a]
"""
    )
    project = load_project(str(tmp_path / "compose.yaml"))
    with pytest.raises(ValueError, match="circular"):
        project.sorted_services()


def test_undefined_network_rejected(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        """
services:
  a:
    image: x
    networks: [nope]
"""
    )
    with pytest.raises(ComposeError, match="undefined network"):
        load_project(str(tmp_path / "compose.yaml"))


def test_windows_path_volume(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        r"""
services:
  a:
    image: x
    volumes:
      - E:\data\www:/srv:ro
"""
    )
    project = load_project(str(tmp_path / "compose.yaml"))
    mount = project.services["a"].volumes[0]
    assert mount.type == "bind"
    assert mount.source == r"E:\data\www"
    assert mount.target == "/srv"
    assert mount.read_only


def test_port_range():
    flags = [p.to_flag() for p in parse_port("8000-8002:9000-9002")]
    assert flags == ["8000:9000", "8001:9001", "8002:9002"]


def test_unsupported_key_warns(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        """
services:
  a:
    image: x
    privileged: true
    restart: always
"""
    )
    project = load_project(str(tmp_path / "compose.yaml"))
    text = "\n".join(project.warnings)
    assert "privileged" in text
    assert "restart" in text


def test_include_single_file_merge_and_precedence(tmp_path):
    (tmp_path / "base.yaml").write_text(
        """
name: from-base
services:
  app:
    image: alpine:3.20
    ports:
      - "8080:80"
    environment:
      FROM_BASE: yes
  worker:
    image: busybox
networks:
  appnet: {}
volumes:
  appdata: {}
"""
    )
    (tmp_path / "compose.yaml").write_text(
        """
include: ./base.yaml
name: from-root
services:
  app:
    image: alpine:3.21
    ports:
      - "9090:80"
    environment:
      FROM_ROOT: yes
"""
    )

    project = load_project(str(tmp_path / "compose.yaml"))

    assert project.name == "from-root"
    assert sorted(project.services.keys()) == ["app", "worker"]
    assert project.services["app"].image == "alpine:3.21"
    assert [p.to_flag() for p in project.services["app"].ports] == ["9090:80"]
    assert project.services["app"].environment == {"FROM_BASE": "True", "FROM_ROOT": "True"}
    assert project.networks["appnet"].name == "from-root_appnet"
    assert project.volumes["appdata"].name == "from-root_appdata"


def test_include_nested(tmp_path):
    (tmp_path / "leaf.yaml").write_text(
        """
services:
  leaf:
    image: alpine
"""
    )
    (tmp_path / "middle.yaml").write_text(
        """
include: ./leaf.yaml
services:
  middle:
    image: busybox
"""
    )
    (tmp_path / "compose.yaml").write_text(
        """
include: ./middle.yaml
services:
  root:
    image: nginx:alpine
"""
    )

    project = load_project(str(tmp_path / "compose.yaml"))
    assert sorted(project.services.keys()) == ["leaf", "middle", "root"]


def test_include_cycle_detected(tmp_path):
    (tmp_path / "a.yaml").write_text(
        """
include: ./b.yaml
services:
  a:
    image: alpine
"""
    )
    (tmp_path / "b.yaml").write_text(
        """
include: ./a.yaml
services:
  b:
    image: alpine
"""
    )

    with pytest.raises(ComposeError, match="include cycle detected"):
        load_project(str(tmp_path / "a.yaml"))


def test_include_missing_file(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        """
include: ./missing.yaml
services:
  app:
    image: alpine
"""
    )

    with pytest.raises(ComposeError, match="include file .*missing.yaml.*compose.yaml"):
        load_project(str(tmp_path / "compose.yaml"))


def test_include_mapping_entry_with_path(tmp_path):
    (tmp_path / "shared.yaml").write_text(
        """
services:
  db:
    image: postgres:16
"""
    )
    (tmp_path / "compose.yaml").write_text(
        """
include:
  - path: ./shared.yaml
services:
  app:
    image: alpine
"""
    )

    project = load_project(str(tmp_path / "compose.yaml"))
    assert sorted(project.services.keys()) == ["app", "db"]


def test_include_unsupported_entry_shape(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        """
include:
  - bad: ./shared.yaml
services:
  app:
    image: alpine
"""
    )

    with pytest.raises(ComposeError, match="unsupported include entry"):
        load_project(str(tmp_path / "compose.yaml"))
