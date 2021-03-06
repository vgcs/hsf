import re
from platform import uname
import subprocess
import click
import os
from logzero import logger
import ruamel.yaml
import pygraphviz as pgv
import shutil

from chal_types import challenge_types, load_chal_from_config, chal_to_kube_config, gen_kube, mkdir_p
from chal_types import GeneratedChallenge, ChallengeHost, ChallengeEnvironment
from gui import App


@click.group()
@click.pass_context
@click.option('--chal-folder', default=None)
def chalgen(ctx, chal_folder):
    ctx.obj = {'chal_folder': chal_folder}


def generate_kube_deploy(kube_dir, trees, local, reg_url):
    configs = []
    for tree in trees:
        def traverse(tree):
            if 'children' not in tree:
                return []

            kube_configs = []
            for child in tree['children']:
                chal = child['chal']
                if chal.container_id:
                    kube_config = chal_to_kube_config(chal, reg_url, local)
                    kube_configs.append(kube_config)

                kube_configs.extend(traverse(child))
            return kube_configs

        chal = tree['chal']
        if chal.container_id:
            kube_config = chal_to_kube_config(chal, reg_url, local)
            configs.append(kube_config)

        chal = tree.get('host')
        if chal and chal.container_id:
            kube_config = chal_to_kube_config(chal, reg_url, local)
            configs.append(kube_config)

        configs.extend(traverse(tree))

    gen_kube(kube_dir, configs, local)

    for kube_config in os.listdir(kube_dir):
        kube_config_path = os.path.join(kube_dir, kube_config)
        os.system(f'kubectl apply -f {kube_config_path} -n challenges')

    return configs


def generate_challenge_graph(trees):
    def traverse(tree):
        if 'children' not in tree or tree['children'] == []:
            return []

        edges = []
        for child in tree['children']:
            edges.append(f'\t"{tree["name"]}"->"{child["name"]}";')
            edges.extend(traverse(child))
        return edges

    edges = []
    for tree in trees:
        edges.extend(traverse(tree))

    formatted_edges = "\n".join(edges)
    graph = f'digraph {{\n{formatted_edges}\n}}'
    G = pgv.AGraph(graph)
    G.layout()
    G.draw('evidence_graph.png')


def get_chal_path_lookup(chals_folder):
    chal_host_folder = os.path.join(chals_folder, 'chal_host')
    if os.path.exists(chal_host_folder):
        shutil.rmtree(chal_host_folder)

    chal_path_lookup = {}
    comp_chals = [d for d in os.listdir(
        chals_folder) if os.path.isdir(os.path.join(chals_folder, d))]
    for comp_chal in comp_chals:
        chal_path = os.path.join(chals_folder, comp_chal)
        chal_config = os.path.join(chal_path, 'chal.yaml')
        assert os.path.exists(chal_config)

        chal = load_chal_from_config(challenge_types, chal_config)
        if type(chal) is str:
            logger.error(
                "Unable to deserialize config, is the challenge bang name correct?")
            return

        chal_path_lookup[chal.name] = chal_path
    return chal_path_lookup


@chalgen.command()
@click.pass_context
@click.option('--chal-config', required=True)
@click.option('--competition-folder', default=None)
def gen(ctx, chal_config, competition_folder):
    chal_dir = os.path.abspath(os.path.dirname(chal_config))

    chal_path_lookup = {}
    if competition_folder is not None:
        competition_folder = os.path.join(os.path.dirname(
            os.path.realpath(__file__)), competition_folder)
        chals_folder = os.path.join(competition_folder, 'chals')
        chal_path_lookup = get_chal_path_lookup(chals_folder)

    chal_gen = load_chal_from_config(challenge_types, chal_config)
    if type(chal_gen) is str:
        logger.error(
            "Unable to deserialize config, is the challenge bang name correct?")
        return

    if chal_gen.config is None:
        logger.error("Please specify 'config' in your challenge config")
        return

    chal_tree = {}
    if ChallengeEnvironment in type(chal_gen).__bases__:
        chal_host = ChallengeHost(
            'http://chal-host.chals.mcpshsf.com', chals_folder)

        chal_gen.chal_host = chal_host
        chal_gen.chal_path_lookup = chal_path_lookup
        chal_gen.challenge_types = challenge_types
        chal_tree = chal_gen.gen_chals(chal_dir)
        chal_tree = [chal_tree]

    chal_gen.do_gen(chal_dir)

    if ChallengeEnvironment in type(chal_gen).__bases__:
        chal_host.create()
    if chal_tree and len(chal_tree) != 0:
        generate_challenge_graph(chal_tree)


def no_reg_url(ctx, param, value):
    if value:
        ctx.command.params[1].required = False
        ctx.command.params[1].default = ""
    return value


@chalgen.command()
@click.pass_context
@click.option('--competition-folder', required=True)
@click.option('--reg-url', required=True, help="Registry to push docker images to")
@click.option('-l', '--local', is_flag=True, default=False, callback=no_reg_url, help="Pass to run the ctf locally(uses minikube)")
def competitiongen(ctx, competition_folder, reg_url, local):
    if local:
        if shutil.which('minikube') is None:
            logger.error("minikube not installed!")
            return
        else:
            os.system('minikube start')
            os.system('minikube addons enable ingress')
            docker_envs = subprocess.check_output(
                ['minikube', 'docker-env', '--shell=cmd']).decode()
            docker_envs = docker_envs.encode('unicode_escape').decode('ascii')
            url = re.search('tcp://127.0.0.1:[0-9]+', docker_envs)
            home_path = re.search('H=.*certs', docker_envs)
            home_path = home_path.group()[2:]
            if 'microsoft-standard' in uname().release:
                home_path = subprocess.check_output(
                    ['wslpath', home_path]).decode()[:-1]
            os.environ['DOCKER_TLS_VERIFY'] = "1"
            os.environ['DOCKER_HOST'] = url.group()
            os.environ['DOCKER_CERT_PATH'] = home_path
            os.environ['MINIKUBE_ACTIVE_DOCKERD'] = 'minikube'
    else:
        logger.info("Please set up your kubernetes cluster before running!")
    os.system('kubectl delete namespace challenges')
    os.system('kubectl create namespace challenges')
    competition_folder = os.path.join(os.path.dirname(
        os.path.realpath(__file__)), competition_folder)
    chals_folder = os.path.join(competition_folder, 'chals')

    competition_config = os.path.join(competition_folder, 'config.yaml')
    assert os.path.exists(competition_config)

    yaml = ruamel.yaml.YAML()
    with open(competition_config, "r") as c:
        comp_config = yaml.load(c)

    chal_path_lookup = get_chal_path_lookup(chals_folder)

    entrypoints = comp_config['entrypoint']

    if type(entrypoints) is str:
        entrypoints = [entrypoints]

    chal_trees = []
    chal_host = ChallengeHost(
        'http://chal-host.chals.mcpshsf.com', chals_folder)

    for entrypoint in entrypoints:
        entrypoint_path = chal_path_lookup[entrypoint]
        entrypoint_config = os.path.join(entrypoint_path, 'chal.yaml')
        chal_gen = load_chal_from_config(challenge_types, entrypoint_config)
        chal_tree = {}

        if ChallengeEnvironment in type(chal_gen).__bases__:
            chal_gen.chal_host = chal_host
            chal_gen.chal_path_lookup = chal_path_lookup
            chal_gen.challenge_types = challenge_types
            chal_tree = chal_gen.gen_chals(entrypoint_path)
            chal_trees.append(chal_tree)

        chal_gen.do_gen(entrypoint_path)

        if GeneratedChallenge in type(chal_gen).__bases__:
            chal_files = chal_gen.chal_file
            if type(chal_files) is not list:
                chal_files = [chal_files]

            logger.info(chal_files)
            for chal_file in chal_files:
                chal_path = os.path.join(
                    chal_path_lookup[chal_gen.name], chal_file)
                chal_url = chal_host.add_chal(chal_path)
                logger.info(f"Generated challenge URL: {chal_url}")

    chal_host.create()
    chal_tree['host'] = chal_host

    if len(chal_trees) != 0:
        kube_dir = os.path.join(competition_folder, 'kube')
        mkdir_p(kube_dir)
        configs = generate_kube_deploy(kube_dir, chal_trees, local, reg_url)
        generate_challenge_graph(chal_trees)
        if local:
            zones_path = os.path.join(os.path.dirname(kube_dir), 'zones.txt')
            if os.path.isfile(zones_path):
                os.remove(zones_path)
            for config in configs:
                url = config['url']
                with open(zones_path, 'a') as z:
                    z.write(f'{url}  A       127.0.0.1\n')
            env_vars = ['DOCKER_TLS_VERIFY', 'DOCKER_HOST',
                        'DOCKER_CERT_PATH', 'MINIKUBE_ACTIVE_DOCKERD']
            for env_var in env_vars:
                os.environ.pop(env_var, None)
            os.system(
                f'docker run --name dnsserver -dp 53:53/udp -p 53:53/tcp -v {zones_path}:/zones/zones.txt samuelcolvin/dnserver')
            logger.info("\nPlease add 127.0.0.1 as a DNS client https://minikube.sigs.k8s.io/docs/handbook/addons/ingress-dns/")
            os.system('minikube tunnel')


@chalgen.command()
@click.pass_context
@click.option('--chal-config', required=True)
def run(ctx, chal_config):
    chal_dir = os.path.dirname(chal_config)
    p = subprocess.Popen(['make', 'run'], cwd=chal_dir)
    p.wait()


@chalgen.command()
@click.pass_context
@click.option('--chal-config', required=True)
def solve(ctx, chal_config):
    chal_gen = load_chal_from_config(challenge_types, chal_config)
    if chal_gen.config is None:
        logger.error("Please specify 'config' in your challenge config")
        return

    chal_dir = os.path.dirname(chal_config)
    solved_flag = chal_gen.solve(chal_dir)
    if solved_flag == chal_gen.flag:
        logger.info("Challenge is solvable")
    else:
        logger.error(
            "Challenge was not solved correctly: {}".format(solved_flag))


@chalgen.command()
@click.pass_context
@click.option('--competition-folder', required=True)
def gui(ctx, competition_folder):
    App(competition_folder).mainloop()


if __name__ == '__main__':
    chalgen()
