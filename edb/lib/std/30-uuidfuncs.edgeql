#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2016-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


## UUID functions and operators.


CREATE FUNCTION
std::uuid_generate_v1mc() -> std::uuid {
    CREATE ANNOTATION std::description := 'Return a version 1 UUID.';
    SET volatility := 'Volatile';
    USING SQL $$
    SELECT edgedbext.uuid_generate_v1mc()
    $$;
};


CREATE INFIX OPERATOR
std::`=` (l: std::uuid, r: std::uuid) -> std::bool {
    SET volatility := 'Immutable';
    SET commutator := 'std::=';
    SET negator := 'std::!=';
    USING SQL OPERATOR r'=';
};


CREATE INFIX OPERATOR
std::`?=` (l: OPTIONAL std::uuid, r: OPTIONAL std::uuid) -> std::bool {
    SET volatility := 'Immutable';
    USING SQL EXPRESSION;
};


CREATE INFIX OPERATOR
std::`!=` (l: std::uuid, r: std::uuid) -> std::bool {
    SET volatility := 'Immutable';
    SET commutator := 'std::!=';
    SET negator := 'std::=';
    USING SQL OPERATOR r'<>';
};


CREATE INFIX OPERATOR
std::`?!=` (l: OPTIONAL std::uuid, r: OPTIONAL std::uuid) -> std::bool {
    SET volatility := 'Immutable';
    USING SQL EXPRESSION;
};


CREATE INFIX OPERATOR
std::`>=` (l: std::uuid, r: std::uuid) -> std::bool {
    SET volatility := 'Immutable';
    SET commutator := 'std::<=';
    SET negator := 'std::<';
    USING SQL OPERATOR '>=';
};


CREATE INFIX OPERATOR
std::`>` (l: std::uuid, r: std::uuid) -> std::bool {
    SET volatility := 'Immutable';
    SET commutator := 'std::<';
    SET negator := 'std::<=';
    USING SQL OPERATOR '>';
};


CREATE INFIX OPERATOR
std::`<=` (l: std::uuid, r: std::uuid) -> std::bool {
    SET volatility := 'Immutable';
    SET commutator := 'std::>=';
    SET negator := 'std::>';
    USING SQL OPERATOR '<=';
};


CREATE INFIX OPERATOR
std::`<` (l: std::uuid, r: std::uuid) -> std::bool {
    SET volatility := 'Immutable';
    SET commutator := 'std::>';
    SET negator := 'std::>=';
    USING SQL OPERATOR '<';
};


## String casts.

CREATE CAST FROM std::str TO std::uuid {
    SET volatility := 'Immutable';
    USING SQL CAST;
};


CREATE CAST FROM std::uuid TO std::str {
    SET volatility := 'Immutable';
    USING SQL CAST;
};
