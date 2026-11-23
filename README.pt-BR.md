# KoreLab

KoreLab é um laboratório local de engenharia e testes para OpenKore e rAthena. Ele ajuda um desenvolvedor ou agente de IA externo a descobrir capacidades, preparar um servidor descartável isolado, executar cenários declarativos usando o comportamento nativo do OpenKore, observar os resultados, verificar o estado persistido de forma independente e limpar os recursos próprios.

KoreLab não inclui um planejador de IA e não substitui OpenKore, rAthena, sua lógica de jogo ou seus repositórios-fonte. O agente planeja e interpreta os experimentos; OpenKore e rAthena continuam sendo as referências para o jogo. As operações de banco de dados do KoreLab se limitam a ambientes rAthena explicitamente configurados e compatíveis com o protocolo MySQL, com alterações protegidas ao estado do laboratório.

## Plataforma e pré-requisitos

- Python 3.10 ou superior está declarado; as verificações desta preparação usaram Python 3.14.7 em macOS arm64. Linux ainda não foi validado. O suporte nativo a Windows x64 está parcialmente implementado, mas ainda não foi verificado em Windows.
- Para inspeção offline de capacidades, basta Python.
- Cenários reais precisam de checkouts locais compatíveis de OpenKore e rAthena. Configure `KORELAB_OPENKORE` e `KORELAB_RATHENA`, ou informe os caminhos nos comandos que os aceitam.
- OpenKore em execução real também exige Perl e XSTools compilado para a mesma combinação de Perl/runtime. `korelab doctor` apresenta os requisitos e achados da toolchain; um diagnóstico aprovado não comprova, sozinho, uma sessão de jogo funcional.
- A integração com rAthena exige um servidor com protocolo MySQL/MariaDB e um driver Python ou cliente compatível. SQL arbitrário não é suportado para rAthena. Forneça credenciais do banco em particular por variáveis de ambiente; `doctor` não conecta sem a opção explícita `--verify-database`.

O runtime usa a biblioteca padrão do Python. Dependências opcionais de testes e MySQL são instaladas separadamente. Relatórios, catálogos, credenciais, locks e saídas de sessão ficam por padrão em `~/.korelab/`, fora do checkout. Use `KORELAB_DATA_HOME` para selecionar outro diretório.

## Instalação e primeira execução

```sh
git clone <URL-do-repositorio-KoreLab>
cd KoreLab
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'

korelab --help
korelab doctor
korelab capabilities
korelab setup                 # mostra um plano de dependências sem alterações
```

O bootstrap para Windows é `bootstrap-windows.ps1`; ele cria um ambiente virtual local ao repositório e pede confirmação antes de usar `winget` para instalar Python. Depois, ative `.venv` e execute `python -m pip install -e '.[dev]'`. Ele não instala nem configura servidores de jogo.

Para uma verificação offline limpa, `korelab capabilities` lista as operações implementadas sem conectar a um servidor. `korelab doctor` inspeciona ferramentas e configuração locais; só verifica conexão com banco quando recebe explicitamente `--verify-database`.

## Laboratório gerenciado e cenários

Consulte as opções exatas antes de operar:

```sh
korelab lab --help
korelab lab plan --help
korelab lab create --help
korelab run --help
```

O ciclo do laboratório é explícito: planejar, criar, iniciar/bootstrap, executar, consultar o estado e então parar ou destruir o laboratório próprio. KoreLab usa registros de propriedade e limpeza com limites; revise o plano e identifique os checkouts e o banco antes de criar. Nunca direcione operações destrutivas a uma instalação não relacionada.

Os cenários são arquivos JSON fornecidos pelo operador. Esta distribuição não inclui instâncias de laboratório, layouts locais de servidor, personagens históricos, fixtures de NPC/quest ou cenários de aceitação. Crie e revise um cenário para seu próprio servidor descartável e informe seu caminho absoluto ao KoreLab:

```sh
korelab plan --help
korelab run /caminho/absoluto/para/cenario.json
```

Para uma frota gerenciada com OpenKore nativo, consulte `korelab lab fleet-run --help`. O comando exige caminhos explícitos para OpenKore/rAthena, overlay de XSTools e ponto inicial. Ele inicia processos OpenKore separados usando o plugin profiles; não simula a lógica de jogo.

## Agentes de IA externos

KoreLab foi projetado para ser operado por um agente de engenharia externo. Comece com `korelab capabilities`, `korelab agent-capabilities` e `korelab technical --help`; gere um catálogo técnico baseado em fontes com `korelab technical refresh --openkore CAMINHO --rathena CAMINHO` quando houver checkouts verificados. O [AGENT_GUIDE.md](AGENT_GUIDE.md) descreve descoberta, execução, verificação independente, investigação de falhas e limpeza.

## Patches, segurança e limitações

Os patches em `patches/` são opcionais e vinculados a versões específicas. Eles não são aplicados automaticamente. Revise origem e compatibilidade; use um worktree descartável e `git apply --check` antes de aplicar. Por padrão, não modifique os checkouts originais dos projetos upstream.

KoreLab não cria contas reais de usuários, troca senhas, configura bancos não relacionados, instala servidores automaticamente nem garante compatibilidade apenas porque um diretório existe. Execução nativa em Windows, Linux e cenários reais em uma instalação externa recém-criada ainda não foram validados para esta preparação. Não alegamos ter validado o cast de Warp Portal no jogo.

Consulte [tests/README.md](tests/README.md) para testes e pré-requisitos. O código e a documentação independentes do KoreLab são licenciados sob MIT, com identificação pública de direitos autorais `boscv`, 2026; consulte [LICENSE](LICENSE). Os patches opcionais para OpenKore e rAthena mantêm os termos upstream aplicáveis, e o módulo Warp Portal adicionado é identificado separadamente. OpenKore e rAthena são projetos distintos e não estão incluídos. Consulte [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) e [LICENSE_REVIEW.md](LICENSE_REVIEW.md) para conhecer os limites exatos e as revisões fixadas.
