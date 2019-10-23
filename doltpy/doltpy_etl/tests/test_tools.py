import io
import pytest
import pandas as pd
from doltpy.doltpy.dolt import Dolt, CREATE, UPDATE
from doltpy.doltpy_etl import (get_df_table_loader,
                               load_to_dolt,
                               insert_unique_key,
                               get_table_transfomer,
                               get_bulk_table_loader)
from doltpy.doltpy.tests.dolt_testing_fixtures import init_repo

MENS_MAJOR_COUNT, WOMENS_MAJOR_COUNT = 'mens_major_count', 'womens_major_count'
AVERAGE_MAJOR_COUNT = 'average_major_count'
INITIAL_WOMENS = pd.DataFrame({'name': ['Serena'], 'major_count': [23]})
INITIAL_MENS = pd.DataFrame({'name': ['Roger'], 'major_count': [20]})
UPDATE_WOMENS = pd.DataFrame({'name': ['Margaret'], 'major_count': [24]})
UPDATE_MENS = pd.DataFrame({'name': ['Rafael'], 'major_count': [19]})


def _populate_test_data_helper(repo: Dolt, mens: pd.DataFrame, womens: pd.DataFrame, branch: str = 'master'):
    table_loaders = [get_df_table_loader(MENS_MAJOR_COUNT, lambda: mens, ['name']),
                     get_df_table_loader(WOMENS_MAJOR_COUNT, lambda: womens, ['name'])]
    load_to_dolt(repo,
                 table_loaders,
                 True,
                 'Loaded {} and {}'.format(MENS_MAJOR_COUNT, WOMENS_MAJOR_COUNT),
                 branch=branch)
    return repo


def _populate_derived_data_helper(repo: Dolt, import_mode: str):
    table_transfomers = [get_table_transfomer(get_raw_data, AVERAGE_MAJOR_COUNT, ['gender'], averager, import_mode)]
    load_to_dolt(repo, table_transfomers, True, 'Updated {}'.format(AVERAGE_MAJOR_COUNT))
    return repo


@pytest.fixture
def initial_test_data(init_repo):
    return _populate_test_data_helper(init_repo, INITIAL_MENS, INITIAL_WOMENS)


@pytest.fixture
def update_test_data(initial_test_data):
    return _populate_test_data_helper(initial_test_data, UPDATE_MENS, UPDATE_WOMENS)


def get_raw_data(repo: Dolt):
    return pd.concat([repo.read_table(MENS_MAJOR_COUNT).assign(gender='mens'),
                      repo.read_table(WOMENS_MAJOR_COUNT).assign(gender='womens')])


def averager(df: pd.DataFrame) -> pd.DataFrame:
    return df.groupby('gender').mean().reset_index()[['gender', 'major_count']].rename(columns={'major_count': 'average'})


@pytest.fixture
def initial_derived_data(initial_test_data):
    return _populate_derived_data_helper(initial_test_data, CREATE)


@pytest.fixture
def update_derived_data(initial_derived_data):
    repo = _populate_test_data_helper(initial_derived_data, UPDATE_MENS, UPDATE_WOMENS)
    return _populate_derived_data_helper(repo, UPDATE)


def test_dataframe_table_loader_create(initial_test_data):
    repo = initial_test_data

    womens_data, mens_data = repo.read_table(WOMENS_MAJOR_COUNT), repo.read_table(MENS_MAJOR_COUNT)
    assert womens_data.iloc[0]['name'] == 'Serena'
    assert mens_data.iloc[0]['name'] == 'Roger'


def test_dataframe_table_loader_update(update_test_data):
    repo = update_test_data

    womens_data, mens_data = repo.read_table(WOMENS_MAJOR_COUNT), repo.read_table(MENS_MAJOR_COUNT)
    assert 'Margaret' in list(womens_data['name'])
    assert 'Rafael' in list(mens_data['name'])


def test_table_transfomer_create(initial_derived_data):
    repo = initial_derived_data
    avg_df = repo.read_table(AVERAGE_MAJOR_COUNT)
    assert avg_df.loc[avg_df['gender'] == 'mens', 'average'].iloc[0] == 20
    assert avg_df.loc[avg_df['gender'] == 'womens', 'average'].iloc[0] == 23


def test_table_transfomer_update(update_derived_data):
    repo = update_derived_data
    avg_df = repo.read_table(AVERAGE_MAJOR_COUNT)
    assert avg_df.loc[avg_df['gender'] == 'mens', 'average'].iloc[0] == (20 + 19) / 2
    assert avg_df.loc[avg_df['gender'] == 'womens', 'average'].iloc[0] == (23 + 24) / 2


def test_insert_unique_key(init_repo):
    repo = init_repo

    def generate_data():
        return pd.DataFrame({'id': [1, 1, 2], 'value': ['foo', 'foo', 'baz']})

    test_table = 'test_data'
    load_to_dolt(repo,
                 [get_df_table_loader(test_table, generate_data, ['hash_id'], transformers=[insert_unique_key])],
                 True,
                 'Updating test data')
    result = repo.read_table(test_table)
    assert result.loc[result['id'] == 1, 'count'].iloc[0] == 2 and 'hash_id' in result.columns


def test_insert_unique_key_column_error():
    with pytest.raises(AssertionError):
        insert_unique_key(pd.DataFrame({'hash_id': ['blah']}))

    with pytest.raises(AssertionError):
        insert_unique_key(pd.DataFrame({'hash_id': ['count']}))


def test_branching(initial_test_data):
    repo = initial_test_data
    test_branch = 'new-branch'
    repo.create_branch(test_branch)
    _populate_test_data_helper(repo, UPDATE_MENS, UPDATE_WOMENS, test_branch)

    assert repo.get_current_branch() == test_branch
    womens_data, mens_data = repo.read_table(WOMENS_MAJOR_COUNT), repo.read_table(MENS_MAJOR_COUNT)
    assert 'Margaret' in list(womens_data['name'])
    assert 'Rafael' in list(mens_data['name'])

    repo.checkout('master')
    womens_data, mens_data = repo.read_table(WOMENS_MAJOR_COUNT), repo.read_table(MENS_MAJOR_COUNT)
    assert 'Margaret' not in list(womens_data['name'])
    assert 'Rafael' not in list(mens_data['name'])


def test_branching_missing_branch(initial_test_data):
    repo = initial_test_data
    test_branch = 'new-branch'
    with pytest.raises(AssertionError):
        _populate_test_data_helper(repo, UPDATE_MENS, UPDATE_WOMENS, test_branch)


CORRUPT_CSV = """player_name,weeks_at_number_1
Roger,Federer,310
Pete Sampras,286
Novak Djokovic,272
Ivan Lendl,270
Jimmy Connors,268
Rafael Nadal,196
John McEnroe,170
Björn Borg,109,,
Andre Agassi,101
Lleyton Hewitt,80
,Stefan Edberg,72
Jim Courier,58
Gustavo Kuerten,43
"""

CLEANED_CSV = """player_name,weeks_at_number_1
Pete Sampras,286
Novak Djokovic,272
Ivan Lendl,270
Jimmy Connors,268
Rafael Nadal,196
John McEnroe,170
Andre Agassi,101
Lleyton Hewitt,80
Jim Courier,58
Gustavo Kuerten,43
"""


def test_get_bulk_table_loader(init_repo):
    repo = init_repo
    table = 'test_table'

    def get_data():
        return io.StringIO(CORRUPT_CSV)

    def cleaner(data: io.StringIO) -> io.StringIO:
        output = io.StringIO()
        header_line = data.readline()
        columns = header_line.split(',')
        output.write(header_line)
        for l in data.readlines():
            if len(l.split(',')) != len(columns):
                print('Corrupt line, discarding:\n{}'.format(l))
            else:
                output.write(l)

        output.seek(0)
        return output

    get_bulk_table_loader(table, get_data, ['player_name'], import_mode=CREATE, transformers=[cleaner])(repo)
    actual = repo.read_table(table)
    expected = io.StringIO(CLEANED_CSV)
    headers = [col.rstrip() for col in expected.readline().split(',')]
    assert all(headers == actual.columns)
    players_to_week_counts = actual.set_index('player_name')['weeks_at_number_1'].to_dict()
    for line in expected.readlines():
        player_name, weeks_at_number_1 = line.split(',')
        assert player_name in players_to_week_counts and players_to_week_counts[player_name] == int(weeks_at_number_1.rstrip())